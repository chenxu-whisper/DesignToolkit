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


def test_despill_recovers_white_foreground_from_green_composites() -> None:
    rgb = np.array([[[64, 255, 64], [128, 255, 128], [191, 255, 191]]], dtype=np.uint8)
    alpha = np.array([[64, 128, 191]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((0.0, 255.0, 0.0)), amount=1.0)

    assert np.all(np.abs(result.astype(np.int16) - 255) <= 1)


def test_despill_preserves_transparent_and_opaque_rgb_exactly() -> None:
    rgb = np.array([[[17, 231, 94], [203, 12, 177]]], dtype=np.uint8)
    alpha = np.array([[0, 255]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((31.0, 149.0, 223.0)), amount=1.0)

    assert np.array_equal(result, rgb)


def test_despill_amount_zero_preserves_entire_image_exactly() -> None:
    rgb = np.array(
        [[[17, 231, 94], [128, 255, 128], [203, 12, 177]]], dtype=np.uint8
    )
    alpha = np.array([[0, 128, 255]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((0.0, 255.0, 0.0)), amount=0.0)

    assert np.array_equal(result, rgb)


@mark.parametrize(
    ("amount", "expected"),
    [
        (0.0, [128, 255, 128]),
        (0.5, [192, 255, 192]),
        (1.0, [255, 255, 255]),
    ],
)
def test_despill_interpolates_toward_recovered_foreground(
    amount: float, expected: list[int]
) -> None:
    rgb = np.array([[[128, 255, 128]]], dtype=np.uint8)
    alpha = np.array([[128]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((0.0, 255.0, 0.0)), amount=amount)

    assert np.all(np.abs(result[0, 0].astype(np.int16) - expected) <= 1)


@mark.parametrize(
    ("background", "foreground"),
    [
        ((20.0, 40.0, 220.0), (240, 180, 100)),
        ((220.0, 30.0, 10.0), (80, 190, 245)),
    ],
)
def test_despill_recovers_full_rgb_for_arbitrary_sampled_background(
    background: tuple[float, float, float], foreground: tuple[int, int, int]
) -> None:
    alpha_value = 128
    alpha_fraction = alpha_value / 255.0
    composite = np.rint(
        alpha_fraction * np.asarray(foreground)
        + (1.0 - alpha_fraction) * np.asarray(background)
    ).astype(np.uint8)

    result = despill(
        composite.reshape(1, 1, 3),
        np.array([[alpha_value]], dtype=np.uint8),
        _model(background),
        amount=1.0,
    )

    assert np.all(np.abs(result[0, 0].astype(np.int16) - foreground) <= 1)


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
