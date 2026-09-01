from math import inf, nan

from pytest import mark, raises

from designtoolkit.chroma_keyer.models import KeyerError
from designtoolkit.chroma_keyer.sampling import (
    map_preview_point,
    screen_model_from_samples,
)


def test_map_preview_point_scales_coordinates_to_source() -> None:
    assert map_preview_point(320, 180, 960, 540, 1920, 1080) == (640, 360)


def test_map_preview_point_clamps_preview_edge_to_source_edge() -> None:
    assert map_preview_point(959.999, 539.999, 960, 540, 1920, 1080) == (
        1919,
        1079,
    )


def test_map_preview_point_scales_fractional_preview_dimensions() -> None:
    assert map_preview_point(400, 200, 400.5, 200.5, 4, 2) == (3, 1)


@mark.parametrize(
    ("arguments", "message"),
    [
        ((0, 0, 0, 540, 1920, 1080), "preview_width must be positive"),
        ((0, 0, 960, -1, 1920, 1080), "preview_height must be positive"),
        ((0, 0, 960, 540, True, 1080), "source_width must be a positive integer"),
        ((0, 0, 960, 540, 1920, 0), "source_height must be a positive integer"),
        ((nan, 0, 960, 540, 1920, 1080), "x must be a finite number"),
        ((inf, 0, 960, 540, 1920, 1080), "x must be a finite number"),
        ((0, nan, 960, 540, 1920, 1080), "y must be a finite number"),
        ((-0.1, 0, 960, 540, 1920, 1080), "x must lie within the preview width"),
        ((960, 0, 960, 540, 1920, 1080), "x must lie within the preview width"),
        ((0, -0.1, 960, 540, 1920, 1080), "y must lie within the preview height"),
        ((0, 540, 960, 540, 1920, 1080), "y must lie within the preview height"),
    ],
)
def test_map_preview_point_rejects_invalid_inputs(
    arguments: tuple[object, ...], message: str
) -> None:
    with raises(KeyerError, match=message):
        map_preview_point(*arguments)  # type: ignore[arg-type]


def test_screen_model_from_samples_returns_the_single_sample_statistics() -> None:
    model = screen_model_from_samples(((10, 20, 30),))

    assert model.mean_rgb == (10.0, 20.0, 30.0)
    assert model.stddev_rgb == (0.0, 0.0, 0.0)
    assert model.samples == ((10, 20, 30),)


def test_screen_model_from_samples_rejects_missing_samples() -> None:
    with raises(KeyerError, match="samples must not be empty"):
        screen_model_from_samples(())


def test_screen_model_from_samples_rejects_multiple_samples() -> None:
    with raises(KeyerError, match="samples must contain exactly one screen color"):
        screen_model_from_samples(((0, 255, 0), (0, 240, 0)))


@mark.parametrize("sample", [(128, 128, 128), (128, 115, 116)])
def test_screen_model_from_samples_rejects_low_saturation(
    sample: tuple[int, int, int],
) -> None:
    with raises(KeyerError, match="请选择具有明显主色的纯色幕背景"):
        screen_model_from_samples((sample,))


def test_screen_model_from_samples_accepts_saturation_at_threshold() -> None:
    assert screen_model_from_samples(((128, 115, 115),)).mean_rgb == (
        128.0,
        115.0,
        115.0,
    )


@mark.parametrize("samples", [((0, 0),), ((0, 0, 256),)])
def test_screen_model_from_samples_rejects_invalid_rgb_surface(
    samples: object,
) -> None:
    with raises(KeyerError):
        screen_model_from_samples(samples)  # type: ignore[arg-type]
