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


def test_generate_alpha_maps_default_green_saturation_ramp() -> None:
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

    assert alpha.dtype == np.uint8
    assert alpha.shape == (1, 5)
    np.testing.assert_array_equal(alpha[0], [0, 64, 128, 191, 255])


@mark.parametrize(
    ("screen_rgb", "ramp"),
    [
        ((0, 255, 0), [[0, 255, 0], [128, 255, 128], [255, 255, 255]]),
        ((0, 0, 255), [[0, 0, 255], [128, 128, 255], [255, 255, 255]]),
        ((255, 0, 0), [[255, 0, 0], [255, 128, 128], [255, 255, 255]]),
    ],
)
def test_generate_alpha_supports_each_primary_screen(
    screen_rgb: tuple[int, int, int], ramp: list[list[int]]
) -> None:
    model = ScreenColorModel(
        mean_rgb=screen_rgb,
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=(screen_rgb,),
    )

    alpha = generate_alpha(
        np.array([ramp], dtype=np.uint8),
        model,
        KeyerParams(),
    )

    np.testing.assert_array_equal(alpha[0], [0, 128, 255])


def test_generate_alpha_screen_gain_two_keys_more() -> None:
    rgb = np.array([[[128, 255, 128]]], dtype=np.uint8)

    default_alpha = generate_alpha(rgb, _green_screen_model(), KeyerParams())
    gain_two_alpha = generate_alpha(
        rgb,
        _green_screen_model(),
        KeyerParams(screen_gain=2.0),
    )

    assert gain_two_alpha[0, 0] < default_alpha[0, 0]


def test_generate_alpha_balance_uses_stronger_and_weaker_other_channels() -> None:
    rgb = np.array([[[128, 255, 64]]], dtype=np.uint8)

    stronger_other_alpha = generate_alpha(
        rgb,
        _green_screen_model(),
        KeyerParams(screen_balance=0.0),
    )
    weaker_other_alpha = generate_alpha(
        rgb,
        _green_screen_model(),
        KeyerParams(screen_balance=1.0),
    )

    assert stronger_other_alpha[0, 0] == 128
    assert weaker_other_alpha[0, 0] == 64


@mark.parametrize("screen_balance", [0.0, 1.0])
def test_generate_alpha_accepts_dominant_green_at_balance_endpoints(
    screen_balance: float,
) -> None:
    alpha = generate_alpha(
        np.array([[[20, 220, 30]]], dtype=np.uint8),
        ScreenColorModel(
            mean_rgb=(20.0, 220.0, 30.0),
            stddev_rgb=(0.0, 0.0, 0.0),
            samples=((20, 220, 30),),
        ),
        KeyerParams(screen_balance=screen_balance),
    )

    assert alpha.tolist() == [[0]]


@mark.parametrize("screen_balance", [0.0, 1.0])
@mark.parametrize("screen_rgb", [(128, 114, 116), (128, 115, 127)])
def test_generate_alpha_rejects_low_dominance_screen_model_independent_of_balance(
    screen_rgb: tuple[int, int, int], screen_balance: float
) -> None:
    model = ScreenColorModel(
        mean_rgb=tuple(float(channel) for channel in screen_rgb),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=(screen_rgb,),
    )

    with raises(KeyerError) as error:
        generate_alpha(
            np.array([[screen_rgb]], dtype=np.uint8),
            model,
            KeyerParams(screen_balance=screen_balance),
        )

    assert str(error.value) == "请选择具有明显主色的纯色幕背景"


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
