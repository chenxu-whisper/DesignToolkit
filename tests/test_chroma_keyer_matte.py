import numpy as np
from pytest import mark, raises

from designtoolkit.chroma_keyer.matte import generate_alpha, refine_alpha
from designtoolkit.chroma_keyer.models import KeyerError, KeyerParams, ScreenColorModel


def _green_screen_model() -> ScreenColorModel:
    return ScreenColorModel(
        mean_rgb=(0.0, 255.0, 0.0),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((0, 255, 0),),
    )


def test_generate_alpha_makes_screen_transparent_and_foreground_opaque() -> None:
    rgb = np.array([[[0, 255, 0], [255, 0, 0]]], dtype=np.uint8)

    alpha = generate_alpha(rgb, _green_screen_model(), KeyerParams(softness=0.1))

    assert alpha.dtype == np.uint8
    assert alpha.shape == (1, 2)
    assert alpha.tolist() == [[0, 255]]


def test_generate_alpha_softness_produces_intermediate_alpha() -> None:
    rgb = np.array([[[0, 192, 0]]], dtype=np.uint8)

    alpha = generate_alpha(
        rgb, _green_screen_model(), KeyerParams(tolerance=0.0, softness=0.5)
    )

    assert 0 < alpha[0, 0] < 255


@mark.parametrize(
    "rgb",
    [
        np.zeros((3, 3), dtype=np.uint8),
        np.zeros((3, 3, 4), dtype=np.uint8),
        np.zeros((3, 3, 3), dtype=np.float32),
    ],
)
def test_generate_alpha_rejects_invalid_rgb_shape_or_dtype(rgb: np.ndarray) -> None:
    with raises(KeyerError):
        generate_alpha(rgb, _green_screen_model(), KeyerParams())


def test_refine_alpha_remaps_clip_black_and_clip_white_endpoints() -> None:
    alpha = np.array([[64, 128, 192]], dtype=np.uint8)

    result = refine_alpha(alpha, KeyerParams(clip_black=0.25, clip_white=0.75))

    assert result.tolist() == [[1, 129, 255]]


@mark.parametrize(
    "alpha",
    [
        np.zeros((3, 3, 1), dtype=np.uint8),
        np.zeros((3, 3), dtype=np.float32),
    ],
)
def test_refine_alpha_rejects_invalid_alpha_shape_or_dtype(alpha: np.ndarray) -> None:
    with raises(KeyerError):
        refine_alpha(alpha, KeyerParams())


@mark.parametrize(
    "params",
    [
        KeyerParams(shrink_grow=1),
        KeyerParams(shrink_grow=-1),
        KeyerParams(feather=1),
    ],
)
def test_refine_alpha_morphology_and_feather_preserve_uint8_shape(
    params: KeyerParams,
) -> None:
    alpha = np.zeros((5, 5), dtype=np.uint8)
    alpha[2, 2] = 255

    result = refine_alpha(alpha, params)

    assert result.shape == alpha.shape
    assert result.dtype == np.uint8
