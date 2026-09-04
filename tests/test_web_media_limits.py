from __future__ import annotations

import warnings
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


def test_rejects_image_above_pixel_limit(tmp_path: Path) -> None:
    image_path = _write_image(tmp_path / "large.png", (11, 10))
    validator = HostedMediaValidator(
        HostedMediaLimits(max_image_pixels=100),
    )

    with pytest.raises(MediaLimitError) as error:
        validator.validate(image_path)

    assert error.value.reason == "image_pixels_exceeded"
    assert "100 pixels" in str(error.value)


def test_treats_pillow_decompression_bomb_warning_as_limit_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "bomb.png"
    image_path.write_bytes(b"header")

    def warn_on_open(_path: Path) -> None:
        warnings.warn(
            "decompression bomb",
            Image.DecompressionBombWarning,
            stacklevel=2,
        )

    monkeypatch.setattr(Image, "open", warn_on_open)
    validator = HostedMediaValidator()

    with pytest.raises(MediaLimitError) as error:
        validator.validate(image_path)

    assert error.value.reason == "image_pixels_exceeded"


def test_accepts_video_at_exact_limits(tmp_path: Path) -> None:
    video_path = tmp_path / "exact.mp4"
    video_path.write_bytes(b"video")
    probe = StubVideoProbe(_metadata())
    validator = HostedMediaValidator(video_probe=probe)

    assert validator.validate(video_path) is probe.metadata
    assert probe.paths == [video_path]


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
