from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
README_TEXT = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
MINIMUM_COMPOSE_VERSION = (2, 6, 0)
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


def test_readme_documents_cross_platform_environment_setup() -> None:
    assert "Linux VPS" in README_TEXT
    assert "cp .env.example .env" in README_TEXT
    assert "Windows PowerShell" in README_TEXT
    assert "Copy-Item .env.example .env" in README_TEXT


def test_readme_upgrade_pulls_only_remote_images() -> None:
    commands = {line.strip() for line in README_TEXT.splitlines()}

    assert "docker compose pull" not in commands
    assert "docker compose pull redis caddy" in commands
    assert "docker compose up -d --build" in commands


def test_resolved_compose_skips_unsupported_compose_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda executable: executable)

    def compose_version(
        command: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["docker", "compose", "version", "--short"]
        return subprocess.CompletedProcess(command, 0, stdout="v2.5.1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", compose_version)

    with pytest.raises(
        pytest.skip.Exception,
        match=r"Docker Compose v2\.6\.0 or newer is required; found v2\.5\.1",
    ):
        _resolved_compose()


def test_resolved_compose_fails_for_invalid_supported_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda executable: executable)

    def compose_command(
        command: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if command == ["docker", "compose", "version", "--short"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="v2.6.0\n",
                stderr="",
            )
        assert command == ["docker", "compose", "config", "--format", "json"]
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="invalid compose configuration",
        )

    monkeypatch.setattr(subprocess, "run", compose_command)

    with pytest.raises(AssertionError, match="invalid compose configuration"):
        _resolved_compose()


def _resolved_compose(
    overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")

    compose_version = subprocess.run(
        ["docker", "compose", "version", "--short"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if compose_version.returncode != 0:
        pytest.skip("Docker Compose is unavailable")
    version_match = re.search(
        r"v?(\d+)\.(\d+)\.(\d+)",
        compose_version.stdout.strip(),
    )
    assert version_match is not None, (
        f"Unable to parse Docker Compose version: {compose_version.stdout!r}"
    )
    installed_version = tuple(int(part) for part in version_match.groups())
    if installed_version < MINIMUM_COMPOSE_VERSION:
        required_version = ".".join(map(str, MINIMUM_COMPOSE_VERSION))
        pytest.skip(
            f"Docker Compose v{required_version} or newer is required; "
            f"found {compose_version.stdout.strip()}"
        )

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
