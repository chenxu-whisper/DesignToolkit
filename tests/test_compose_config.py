from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_VARIABLES = (
    "APP_CPUS",
    "APP_MEMORY",
    "APP_PIDS",
    "WORKER_CPUS",
    "WORKER_MEMORY",
    "WORKER_PIDS",
    "REDIS_CPUS",
    "REDIS_MEMORY",
    "REDIS_PIDS",
    "CLEANUP_CPUS",
    "CLEANUP_MEMORY",
    "CLEANUP_PIDS",
    "CADDY_CPUS",
    "CADDY_MEMORY",
    "CADDY_PIDS",
)


def _resolved_compose(
    overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")

    compose_version = subprocess.run(
        ["docker", "compose", "version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if compose_version.returncode != 0:
        pytest.skip("Docker Compose is unavailable")

    environment = os.environ.copy()
    environment["DOMAIN"] = "tools.example.com"
    environment.update(dict.fromkeys(RESOURCE_VARIABLES, ""))
    environment.update(overrides or {})
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("service_name", "cpus", "mem_limit", "pids_limit"),
    [
        ("app", 1.0, 1_610_612_736, 256),
        ("worker-1", 3.0, 5_368_709_120, 256),
        ("worker-2", 3.0, 5_368_709_120, 256),
        ("redis", 0.5, 536_870_912, 128),
        ("cleanup", 0.25, 268_435_456, 128),
        ("caddy", 0.25, 268_435_456, 128),
    ],
)
def test_compose_resolves_default_resource_limits(
    service_name: str,
    cpus: float,
    mem_limit: int,
    pids_limit: int,
) -> None:
    services = _resolved_compose()["services"]

    assert services[service_name].get("cpus") == cpus
    assert int(services[service_name].get("mem_limit", -1)) == mem_limit
    assert services[service_name].get("pids_limit") == pids_limit


def test_compose_resource_limits_allow_environment_overrides() -> None:
    services = _resolved_compose({"APP_CPUS": "1.5"})["services"]

    assert services["app"]["cpus"] == 1.5
