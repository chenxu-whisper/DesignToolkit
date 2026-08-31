from math import inf, nan

import numpy as np
from pytest import mark, raises

from designtoolkit.chroma_keyer.despill import despill
from designtoolkit.chroma_keyer.models import KeyerError, ScreenColorModel


def _model(mean_rgb: tuple[float, float, float]) -> ScreenColorModel:
    return ScreenColorModel(
        mean_rgb=mean_rgb,
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((10, 10, 10),),
    )


def test_despill_suppresses_green_on_fractional_alpha_edge() -> None:
    rgb = np.array([[[100, 200, 120]]], dtype=np.uint8)
    alpha = np.array([[128]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((10.0, 200.0, 10.0)), amount=1.0)

    assert result[0, 0, 0] == 100
    assert result[0, 0, 1] < 200
    assert result[0, 0, 2] == 120


def test_despill_strongly_suppresses_green_on_low_alpha_edge() -> None:
    rgb = np.array([[[100, 200, 180]]], dtype=np.uint8)
    alpha = np.array([[48]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((10.0, 200.0, 10.0)), amount=1.0)

    assert result[0, 0, 0] == 100
    assert result[0, 0, 1] <= 185
    assert result[0, 0, 2] == 180


def test_despill_preserves_transparent_and_opaque_pixels_byte_identical() -> None:
    rgb = np.array(
        [[[10, 200, 10], [100, 200, 180], [20, 190, 30]]], dtype=np.uint8
    )
    alpha = np.array([[0, 48, 255]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((10.0, 200.0, 10.0)), amount=1.0)

    assert np.array_equal(result[:, [0, 2]], rgb[:, [0, 2]])


def test_despill_suppresses_dominant_blue_channel() -> None:
    rgb = np.array([[[100, 120, 200]]], dtype=np.uint8)

    result = despill(
        rgb,
        np.array([[128]], dtype=np.uint8),
        _model((10.0, 10.0, 200.0)),
        amount=1.0,
    )

    assert result[0, 0, 2] < rgb[0, 0, 2]
    assert result[0, 0, :2].tolist() == [100, 120]


def test_despill_neutral_screen_model_uses_sample_luminance_as_target() -> None:
    rgb = np.array([[[200, 100, 100]]], dtype=np.uint8)

    result = despill(
        rgb,
        np.array([[128]], dtype=np.uint8),
        _model((100.0, 105.0, 100.0)),
        amount=1.0,
    )

    assert result[0, 0].tolist() == [141, 101, 101]


def test_despill_red_dominant_screen_model_uses_neutral_fallback() -> None:
    rgb = np.array([[[200, 100, 100]]], dtype=np.uint8)

    result = despill(
        rgb,
        np.array([[128]], dtype=np.uint8),
        _model((200.0, 10.0, 10.0)),
        amount=1.0,
    )

    assert result[0, 0].tolist() == [124, 84, 84]


@mark.parametrize(
    ("rgb", "alpha", "amount"),
    [
        (
            np.zeros((2, 2), dtype=np.uint8),
            np.zeros((2, 2), dtype=np.uint8),
            0.5,
        ),
        (
            np.zeros((2, 2, 3), dtype=np.float32),
            np.zeros((2, 2), dtype=np.uint8),
            0.5,
        ),
        (
            np.zeros((2, 2, 3), dtype=np.uint8),
            np.zeros((2, 2, 1), dtype=np.uint8),
            0.5,
        ),
        (
            np.zeros((2, 2, 3), dtype=np.uint8),
            np.zeros((3, 2), dtype=np.uint8),
            0.5,
        ),
        (
            np.zeros((2, 2, 3), dtype=np.uint8),
            np.zeros((2, 2), dtype=np.uint8),
            nan,
        ),
        (
            np.zeros((2, 2, 3), dtype=np.uint8),
            np.zeros((2, 2), dtype=np.uint8),
            inf,
        ),
        (
            np.zeros((2, 2, 3), dtype=np.uint8),
            np.zeros((2, 2), dtype=np.uint8),
            1.1,
        ),
    ],
)
def test_despill_rejects_invalid_inputs(
    rgb: np.ndarray, alpha: np.ndarray, amount: float
) -> None:
    with raises(KeyerError):
        despill(rgb, alpha, _model((10.0, 200.0, 10.0)), amount)
