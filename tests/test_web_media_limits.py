from __future__ import annotations

import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from designtoolkit.web.hosted import HostedSettings
from designtoolkit.web.media_limits import (
    HostedMediaLimits,
    HostedMediaValidator,
    MediaLimitError,
)


class StubVideoProbe:
    def __init__(self, metadata: object = None, error: Exception | None = None) -> None:
        self.metadata = metadata
        self.error = error
        self.paths: list[Path] = []

    def __call__(self, path: Path) -> object:
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        return self.metadata


def _metadata(
    *,
    width: object = 3840,
    height: object = 2160,
    duration_seconds: object = 1800.0,
) -> object:
    return SimpleNamespace(
        width=width,
        height=height,
        duration_seconds=duration_seconds,
    )


def _write_image(path: Path, size: tuple[int, int]) -> Path:
    Image.new("RGB", size).save(path)
    return path


def _write_animated_gif(path: Path) -> Path:
    frames = [
        Image.new("RGB", (2, 2), color)
        for color in ((255, 0, 0), (0, 255, 0))
    ]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=10)
    return path


def _write_png_header(path: Path, size: tuple[int, int]) -> Path:
    width, height = size

    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", b"")
        + chunk(b"IEND", b"")
    )
    return path


def test_hosted_media_limits_are_frozen_with_compiled_defaults() -> None:
    limits = HostedMediaLimits()

    assert limits == HostedMediaLimits(
        max_image_pixels=100_000_000,
        max_video_width=3840,
        max_video_height=2160,
        max_video_duration_seconds=1800.0,
    )
    with pytest.raises(AttributeError):
        limits.max_image_pixels = 1  # type: ignore[misc]


def test_accepts_image_at_exact_pixel_limit(tmp_path: Path) -> None:
    image_path = _write_image(tmp_path / "exact.png", (10, 10))
    validator = HostedMediaValidator(
        HostedMediaLimits(max_image_pixels=100),
    )

    assert validator.validate(image_path) is None


@pytest.mark.parametrize("suffix", [".gif", ".webp"])
def test_accepts_static_multiframe_capable_image(
    tmp_path: Path,
    suffix: str,
) -> None:
    image_path = _write_image(tmp_path / f"static{suffix}", (2, 2))

    assert HostedMediaValidator().validate(image_path) is None


def test_rejects_animated_gif_from_header_metadata(tmp_path: Path) -> None:
    image_path = _write_animated_gif(tmp_path / "animated.gif")

    with pytest.raises(MediaLimitError) as error:
        HostedMediaValidator().validate(image_path)

    assert error.value.reason == "animated_image"


@pytest.mark.parametrize("suffix", [".png", ".webp"])
def test_rejects_simulated_animated_image_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    image_path = tmp_path / f"animated{suffix}"
    image_path.write_bytes(b"header")
    header = SimpleNamespace(size=(2, 2), n_frames=2)
    monkeypatch.setattr(Image, "open", lambda _path: nullcontext(header))

    with pytest.raises(MediaLimitError) as error:
        HostedMediaValidator().validate(image_path)

    assert error.value.reason == "animated_image"


def test_rejects_image_above_pixel_limit(tmp_path: Path) -> None:
    image_path = _write_image(tmp_path / "large.png", (11, 10))
    validator = HostedMediaValidator(
        HostedMediaLimits(max_image_pixels=100),
    )

    with pytest.raises(MediaLimitError) as error:
        validator.validate(image_path)

    assert error.value.reason == "image_pixels_exceeded"
    assert "100 pixels" in str(error.value)


def test_accepts_real_image_header_above_pillow_warning_threshold(
    tmp_path: Path,
) -> None:
    image_path = _write_png_header(tmp_path / "allowed.png", (10_000, 9_000))

    assert HostedMediaValidator().validate(image_path) is None


def test_rejects_real_image_header_above_hosted_pixel_limit(tmp_path: Path) -> None:
    image_path = _write_png_header(tmp_path / "too-large.png", (10_001, 10_000))

    with pytest.raises(MediaLimitError) as error:
        HostedMediaValidator().validate(image_path)

    assert error.value.reason == "image_pixels_exceeded"
    assert "100000000 pixels" in str(error.value)


def test_treats_pillow_decompression_bomb_error_as_limit_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "bomb.png"
    image_path.write_bytes(b"header")

    def fail_on_open(_path: Path) -> None:
        raise Image.DecompressionBombError("decompression bomb")

    monkeypatch.setattr(Image, "open", fail_on_open)
    with pytest.raises(MediaLimitError) as error:
        HostedMediaValidator().validate(image_path)

    assert error.value.reason == "image_pixels_exceeded"


def test_accepts_video_at_exact_limits(tmp_path: Path) -> None:
    video_path = tmp_path / "exact.mp4"
    video_path.write_bytes(b"video")
    probe = StubVideoProbe(_metadata())
    validator = HostedMediaValidator(video_probe=probe)

    assert validator.validate(video_path) is probe.metadata
    assert probe.paths == [video_path]


def test_validate_many_deduplicates_identical_resolved_paths(
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "exact.mp4"
    video_path.write_bytes(b"video")
    probe = StubVideoProbe(_metadata())
    validator = HostedMediaValidator(video_probe=probe)
    aliases = [video_path] * 25

    results = validator.validate_many(aliases)

    assert results == [probe.metadata] * 25
    assert probe.paths == [video_path.resolve()]


def test_validate_video_reuses_cached_metadata_until_stat_changes(
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    probe = StubVideoProbe(_metadata())
    validator = HostedMediaValidator(video_probe=probe)

    assert validator.validate_video(video_path) is probe.metadata
    assert validator.validate_video(video_path) is probe.metadata
    video_path.write_bytes(b"changed-video")
    assert validator.validate_video(video_path) is probe.metadata

    assert probe.paths == [video_path.resolve(), video_path.resolve()]


def test_metadata_cache_is_bounded_lru(tmp_path: Path) -> None:
    paths = [tmp_path / f"clip-{index}.mp4" for index in range(3)]
    for path in paths:
        path.write_bytes(b"video")
    probe = StubVideoProbe(_metadata())
    validator = HostedMediaValidator(video_probe=probe, cache_size=2)

    validator.validate(paths[0])
    validator.validate(paths[1])
    validator.validate(paths[0])
    validator.validate(paths[2])
    validator.validate(paths[1])

    assert probe.paths == [
        paths[0].resolve(),
        paths[1].resolve(),
        paths[2].resolve(),
        paths[1].resolve(),
    ]


def test_metadata_cache_serializes_concurrent_validation(tmp_path: Path) -> None:
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    metadata = _metadata()
    probe_calls = 0
    probe_lock = threading.Lock()

    def probe(_path: Path) -> object:
        nonlocal probe_calls
        with probe_lock:
            probe_calls += 1
        time.sleep(0.01)
        return metadata

    validator = HostedMediaValidator(video_probe=probe)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(validator.validate_video, [video_path] * 25))

    assert results == [metadata] * 25
    assert probe_calls == 1


@pytest.mark.parametrize(
    ("metadata", "reason", "message"),
    [
        (_metadata(width=3841), "video_dimensions_exceeded", "3840x2160"),
        (_metadata(height=2161), "video_dimensions_exceeded", "3840x2160"),
        (_metadata(duration_seconds=1800.1), "video_duration_exceeded", "1800"),
    ],
)
def test_rejects_video_above_compiled_limits(
    tmp_path: Path,
    metadata: object,
    reason: str,
    message: str,
) -> None:
    video_path = tmp_path / "large.mp4"
    video_path.write_bytes(b"video")
    validator = HostedMediaValidator(video_probe=StubVideoProbe(metadata))

    with pytest.raises(MediaLimitError) as error:
        validator.validate(video_path)

    assert error.value.reason == reason
    assert message in str(error.value)


@pytest.mark.parametrize(
    "metadata",
    [
        _metadata(width=0),
        _metadata(height=-1),
        _metadata(duration_seconds=0),
        _metadata(duration_seconds=float("nan")),
        _metadata(duration_seconds=float("inf")),
    ],
)
def test_rejects_invalid_video_metadata(
    tmp_path: Path,
    metadata: object,
) -> None:
    video_path = tmp_path / "invalid.mp4"
    video_path.write_bytes(b"video")
    validator = HostedMediaValidator(video_probe=StubVideoProbe(metadata))

    with pytest.raises(MediaLimitError) as error:
        validator.validate(video_path)

    assert error.value.reason == "invalid_media"
    assert "metadata" in str(error.value).lower()


@pytest.mark.parametrize("suffix", [".png", ".mp4"])
def test_rejects_unreadable_media(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"broken{suffix}"
    path.write_bytes(b"not media")
    validator = HostedMediaValidator(
        video_probe=StubVideoProbe(error=ValueError("unreadable")),
    )

    with pytest.raises(MediaLimitError) as error:
        validator.validate(path)

    assert error.value.reason == "invalid_media"
    assert "could not be read" in str(error.value).lower()


def test_rejects_unsupported_suffix_without_probing(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"not media")
    probe = StubVideoProbe(_metadata())
    validator = HostedMediaValidator(video_probe=probe)

    with pytest.raises(MediaLimitError) as error:
        validator.validate(path)

    assert error.value.reason == "invalid_media"
    assert "unsupported" in str(error.value).lower()
    assert probe.paths == []


def test_hosted_settings_allow_lower_media_limits() -> None:
    settings = HostedSettings.from_env(
        {
            "DESIGNTOOLKIT_MAX_IMAGE_PIXELS": "90000000",
            "DESIGNTOOLKIT_MAX_VIDEO_WIDTH": "1920",
            "DESIGNTOOLKIT_MAX_VIDEO_HEIGHT": "1080",
            "DESIGNTOOLKIT_MAX_VIDEO_DURATION_SECONDS": "900.5",
        }
    )

    assert settings.max_image_pixels == 90_000_000
    assert settings.max_video_width == 1920
    assert settings.max_video_height == 1080
    assert settings.max_video_duration_seconds == 900.5


def test_hosted_settings_clamp_media_limits_to_compiled_ceilings() -> None:
    settings = HostedSettings.from_env(
        {
            "DESIGNTOOLKIT_MAX_IMAGE_PIXELS": "100000001",
            "DESIGNTOOLKIT_MAX_VIDEO_WIDTH": "7680",
            "DESIGNTOOLKIT_MAX_VIDEO_HEIGHT": "4320",
            "DESIGNTOOLKIT_MAX_VIDEO_DURATION_SECONDS": "1800.1",
        }
    )

    assert settings.max_image_pixels == 100_000_000
    assert settings.max_video_width == 3840
    assert settings.max_video_height == 2160
    assert settings.max_video_duration_seconds == 1800.0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DESIGNTOOLKIT_MAX_IMAGE_PIXELS", "0"),
        ("DESIGNTOOLKIT_MAX_VIDEO_WIDTH", "-1"),
        ("DESIGNTOOLKIT_MAX_VIDEO_HEIGHT", "0"),
        ("DESIGNTOOLKIT_MAX_VIDEO_DURATION_SECONDS", "nan"),
    ],
)
def test_hosted_settings_require_positive_finite_media_limits(
    name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=f"{name} must be positive"):
        HostedSettings.from_env({name: value})
