import numpy as np
from pytest import mark, raises

from designtoolkit.chroma_keyer.matte import generate_alpha, refine_alpha
from designtoolkit.chroma_keyer.models import KeyerError, KeyerParams, ScreenColorModel
from designtoolkit.chroma_keyer.sampling import screen_model_from_samples


def _green_screen_model() -> ScreenColorModel:
    return ScreenColorModel(
        mean_rgb=(0.0, 255.0, 0.0),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((0, 255, 0),),
    )


def test_generate_alpha_projects_sampled_green_chroma() -> None:
    rgb = np.array([[[0, 255, 0], [128, 255, 128], [255, 0, 0]]], dtype=np.uint8)

    alpha = generate_alpha(
        rgb, _green_screen_model(), KeyerParams(tolerance=0.0, softness=1.0)
    )

    assert alpha.dtype == np.uint8
    assert alpha.shape == (1, 3)
    assert alpha[0, 0] == 0
    assert 0 < alpha[0, 1] < 255
    assert alpha[0, 2] == 255


def test_generate_alpha_smoothly_remaps_default_green_projection() -> None:
    rgb = np.array(
        [
            [
                [0, 255, 0],
                [64, 255, 64],
                [128, 255, 128],
                [191, 255, 191],
                [255, 255, 255],
            ]
        ],
        dtype=np.uint8,
    )

    alpha = generate_alpha(rgb, _green_screen_model(), KeyerParams())

    np.testing.assert_array_equal(alpha[0], [0, 40, 128, 215, 255])
    assert np.all(np.diff(alpha[0].astype(np.int16)) > 0)


def test_generate_alpha_applies_tolerance_and_softness_to_green_projection() -> None:
    rgb = np.array([[[128, 255, 128]]], dtype=np.uint8)

    default_alpha = int(generate_alpha(rgb, _green_screen_model(), KeyerParams())[0, 0])
    tolerant_alpha = int(
        generate_alpha(
            rgb,
            _green_screen_model(),
            KeyerParams(tolerance=0.25, softness=1.0),
        )[0, 0]
    )
    firm_alpha = int(
        generate_alpha(
            rgb,
            _green_screen_model(),
            KeyerParams(tolerance=0.0, softness=0.25),
        )[0, 0]
    )

    assert tolerant_alpha < default_alpha < firm_alpha


def test_generate_alpha_keeps_perpendicular_chroma_as_foreground() -> None:
    rgb = np.array([[[0, 220, 255]]], dtype=np.uint8)

    alpha = generate_alpha(
        rgb, _green_screen_model(), KeyerParams(tolerance=0.0, softness=1.0)
    )

    assert alpha[0, 0] > 200


def test_generate_alpha_falls_back_to_normalized_rgb_distance_for_gray_screen() -> None:
    rgb = np.array([[[133, 0, 0]]], dtype=np.uint8)
    model = ScreenColorModel(
        mean_rgb=(0.0, 0.0, 0.0),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((0, 0, 0),),
    )

    alpha = generate_alpha(rgb, model, KeyerParams(tolerance=0.25, softness=0.15))

    assert alpha[0, 0] == 69


def test_generate_alpha_falls_back_for_near_neutral_screen_chroma() -> None:
    rgb = np.array([[[128, 128, 128]]], dtype=np.uint8)
    model = ScreenColorModel(
        mean_rgb=(128.0, 128.0 + 1e-5, 128.0 - 1e-5),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((128, 128, 128),),
    )

    alpha = generate_alpha(rgb, model, KeyerParams(tolerance=0.0, softness=0.15))

    assert alpha[0, 0] == 0


def test_generate_alpha_falls_back_for_low_chroma_sample_model() -> None:
    model = screen_model_from_samples(((128, 128, 128),) * 9_999 + ((128, 129, 127),))

    alpha = generate_alpha(
        np.array([[[128, 128, 128]]], dtype=np.uint8),
        model,
        KeyerParams(tolerance=0.0, softness=1.0),
    )

    assert alpha[0, 0] == 0


def test_generate_alpha_blends_neutral_chroma_projection_confidence() -> None:
    gray_rgb = np.array([[[128, 128, 128]]], dtype=np.uint8)
    models = [
        ScreenColorModel(
            mean_rgb=mean_rgb,
            stddev_rgb=(0.0, 0.0, 0.0),
            samples=((128, 128, 128),),
        )
        for mean_rgb in ((122.0, 128.0, 134.0), (121.0, 128.0, 135.0))
    ]

    alpha_values = [
        int(generate_alpha(gray_rgb, model, KeyerParams())[0, 0])
        for model in models
    ]

    assert all(0 < alpha < 128 for alpha in alpha_values)
    assert abs(alpha_values[1] - alpha_values[0]) < 32


def test_generate_alpha_avoids_adjacent_low_chroma_default_cliff() -> None:
    rgb = np.array([[[16, 0, 224]]], dtype=np.uint8)
    models = [
        ScreenColorModel(
            mean_rgb=mean_rgb,
            stddev_rgb=(0.0, 0.0, 0.0),
            samples=(tuple(int(channel) for channel in mean_rgb),),
        )
        for mean_rgb in ((112.0, 112.0, 144.0), (113.0, 112.0, 144.0))
    ]

    alpha_values = [
        int(generate_alpha(rgb, model, KeyerParams())[0, 0]) for model in models
    ]

    assert all(0 < alpha < 255 for alpha in alpha_values)
    assert abs(alpha_values[1] - alpha_values[0]) < 32


def test_generate_alpha_stabilizes_low_chroma_perpendicular_residual() -> None:
    model = ScreenColorModel(
        mean_rgb=(116.0, 128.0, 140.0),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((116, 128, 140),),
    )
    rgb = np.array([[[128, 156, 152]]], dtype=np.uint8)

    alpha = generate_alpha(
        rgb, model, KeyerParams(tolerance=0.0, softness=1.0)
    )

    assert alpha[0, 0] == 72


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


def test_refine_alpha_negative_shrink_erodes_continuous_center_value() -> None:
    alpha = np.array([[64, 64, 64], [64, 128, 255], [64, 64, 64]], dtype=np.uint8)

    result = refine_alpha(alpha, KeyerParams(shrink_grow=-1))

    assert result[1, 1] == 96
    assert result[1, 1] < alpha[1, 1]


def test_refine_alpha_positive_growth_dilates_continuous_center_value() -> None:
    alpha = np.array([[0, 0, 0], [0, 128, 255], [0, 0, 0]], dtype=np.uint8)

    result = refine_alpha(alpha, KeyerParams(shrink_grow=1))

    assert result[1, 1] == 192
    assert result[0, 1] == 64
    assert result[1, 1] > alpha[1, 1]


def test_refine_alpha_clips_before_float_morphology_and_feathers_afterward() -> None:
    alpha = np.array(
        [
            [0, 64, 128, 192, 255],
            [32, 96, 160, 224, 16],
            [48, 112, 176, 240, 80],
            [208, 144, 100, 56, 24],
            [255, 192, 128, 64, 0],
        ],
        dtype=np.uint8,
    )
    expected = np.array(
        [
            [56, 104, 192, 227, 223],
            [84, 127, 204, 223, 209],
            [150, 169, 203, 192, 165],
            [215, 205, 179, 128, 94],
            [243, 222, 167, 93, 52],
        ],
        dtype=np.uint8,
    )

    result = refine_alpha(
        alpha,
        KeyerParams(clip_black=0.25, clip_white=0.75, shrink_grow=1, feather=1),
    )

    np.testing.assert_array_equal(result, expected)


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


def test_refine_alpha_rejects_empty_alpha_for_morphology_and_feather() -> None:
    with raises(KeyerError):
        refine_alpha(np.empty((0, 0), dtype=np.uint8), KeyerParams(shrink_grow=1))

    with raises(KeyerError):
        refine_alpha(np.empty((0, 0), dtype=np.uint8), KeyerParams(shrink_grow=-1))

    with raises(KeyerError):
        refine_alpha(np.empty((0, 0), dtype=np.uint8), KeyerParams(feather=1))


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
