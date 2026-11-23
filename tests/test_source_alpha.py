import importlib
import subprocess
import sys

import numpy as np
import pytest

from designtoolkit.chroma_keyer.models import KeyerError
from test_source_alpha_export import make_native


def source_module():
    try:
        return importlib.import_module("designtoolkit.chroma_keyer.source_alpha")
    except ModuleNotFoundError:
        pytest.fail("source_alpha helper is not implemented")


def test_split_preserves_identical_green_with_different_alpha():
    frame = np.array([[[20, 220, 40, a] for a in (0, 128, 255)]], dtype=np.uint8)
    original = frame.copy()
    rgb, alpha = source_module().split_source_rgba(frame)
    np.testing.assert_array_equal(rgb, original[..., :3])
    np.testing.assert_array_equal(alpha, original[..., 3])
    np.testing.assert_array_equal(frame, original)


def test_split_premultiplied_restores_color_with_zero_guard():
    frame = np.array([[[64, 32, 128, 128], [200, 50, 20, 0]]], dtype=np.uint8)
    rgb, alpha = source_module().split_source_rgba(frame, "premultiplied")
    np.testing.assert_array_equal(rgb, [[[128, 64, 255], [0, 0, 0]]])
    np.testing.assert_array_equal(alpha, [[128, 0]])


@pytest.mark.parametrize(
    "frame",
    [
        None,
        np.zeros((1, 2, 3), dtype=np.uint8),
        np.zeros((1, 2, 4), dtype=np.float32),
        np.zeros((1, 2, 4), dtype=np.uint16),
        np.zeros((0, 2, 4), dtype=np.uint8),
        np.zeros((2, 4), dtype=np.uint8),
    ],
)
def test_split_rejects_invalid_rgba_before_casting(frame):
    with pytest.raises(KeyerError, match="RGBA|uint8"):
        source_module().split_source_rgba(frame)


def test_split_rejects_unknown_mode():
    with pytest.raises(KeyerError, match="mode"):
        source_module().split_source_rgba(np.zeros((1, 1, 4), np.uint8), "guess")


def read_preview(*args, **kwargs):
    reader = getattr(source_module(), "read_source_preview", None)
    assert callable(reader), "native alpha preview reader is not implemented"
    return reader(*args, **kwargs)


@pytest.mark.parametrize("codec", ["qtrle", "prores_ks"])
def test_preview_reads_native_alpha_at_original_dimensions(tmp_path, codec):
    path, frame = make_native(tmp_path / "native.mov", codec)
    rgb, alpha = read_preview(path, 0.3)
    assert rgb.shape == (16, 32, 3)
    np.testing.assert_allclose(rgb[8, 4], [20, 220, 40], atol=2)
    np.testing.assert_allclose(alpha, frame[..., 3], atol=1)


def test_preview_accepts_opaque_native_alpha_but_rejects_rgb_mov(tmp_path):
    opaque, _ = make_native(tmp_path / "opaque.mov", opaque=True)
    _, alpha = read_preview(opaque, 0.0)
    assert np.all(alpha == 255)
    rgb_path, _ = make_native(tmp_path / "rgb.mov", "libx264")
    with pytest.raises(KeyerError, match="native.*alpha"):
        read_preview(rgb_path, 0.0)


@pytest.mark.parametrize("timestamp", [-1, float("inf"), float("nan"), True, "0"])
def test_preview_rejects_invalid_timestamp(tmp_path, timestamp):
    with pytest.raises(KeyerError, match="timestamp"):
        read_preview(tmp_path / "native.mov", timestamp)


def test_preview_timeout_kills_and_reaps_child(tmp_path, monkeypatch):
    module = source_module()
    original = subprocess.Popen
    processes = []

    def stall(command, **kwargs):
        process = original(
            [sys.executable, "-c", "import time;time.sleep(30)"], **kwargs
        )
        processes.append(process)
        return process

    path, _ = make_native(tmp_path / "native.mov")
    monkeypatch.setattr(module, "_PREVIEW_TIMEOUT_SECONDS", 0.15, raising=False)
    monkeypatch.setattr(subprocess, "Popen", stall)
    with pytest.raises(KeyerError, match="timed out"):
        read_preview(path, 0.0)
    assert processes and all(p.poll() is not None for p in processes)
    assert all(p.stdout.closed and p.stderr.closed for p in processes)


def test_preview_stderr_is_bounded_on_failure(tmp_path, monkeypatch):
    original = subprocess.Popen
    path, _ = make_native(tmp_path / "native.mov")

    def noisy(command, **kwargs):
        return original(
            [
                sys.executable,
                "-c",
                "import sys;sys.stderr.write('x'*200000+'decode failed');sys.exit(7)",
            ],
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", noisy)
    with pytest.raises(KeyerError, match="decode failed") as error:
        read_preview(path, 0.0)
    assert len(str(error.value)) < 70000


def test_preview_dimension_limit_checked_before_rgba_decode(tmp_path, monkeypatch):
    module = source_module()
    path, _ = make_native(tmp_path / "native.mov")
    monkeypatch.setattr(module, "_MAX_PREVIEW_DIMENSION", 16, raising=False)
    with pytest.raises(KeyerError, match="dimensions"):
        read_preview(path, 0.0)


def test_preview_stdout_is_bounded_and_reaps_child(tmp_path, monkeypatch):
    original = subprocess.Popen
    path, _ = make_native(tmp_path / "native.mov")
    processes = []

    def overflowing(command, **kwargs):
        if "rgba" in command:
            command = [
                sys.executable,
                "-c",
                "import sys,time;sys.stdout.buffer.write(b'x'*200000);"
                "sys.stdout.flush();time.sleep(30)",
            ]
        process = original(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", overflowing)
    with pytest.raises(KeyerError, match="byte length|output limit"):
        read_preview(path, 0.0)
    assert processes and all(p.poll() is not None for p in processes)
