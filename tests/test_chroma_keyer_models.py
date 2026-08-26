from math import inf, nan
from pathlib import Path

from pytest import mark, raises

from designtoolkit.chroma_keyer.models import (
    KeyerError,
    KeyerParams,
    KeyImageOptions,
    KeyImageResult,
    ScreenColorModel,
)


@mark.parametrize(
    ("clip_black", "clip_white"),
    [
        (0.8, 0.7),
        (0.8, 0.8),
    ],
)
def test_params_reject_clip_black_not_less_than_clip_white(
    clip_black: float, clip_white: float
) -> None:
    with raises(KeyerError) as error:
        KeyerParams(clip_black=clip_black, clip_white=clip_white)

    assert str(error.value) == "clip_black must be less than clip_white"


def test_params_accept_default_mvp_values() -> None:
    assert KeyerParams() == KeyerParams(
        tolerance=0.25,
        softness=0.15,
        clip_black=0.0,
        clip_white=1.0,
        shrink_grow=0,
        feather=0,
        despill_amount=0.5,
    )


@mark.parametrize(
    ("field", "value"),
    [
        ("tolerance", -0.01),
        ("tolerance", 1.01),
        ("softness", -0.01),
        ("softness", 1.01),
        ("clip_black", -0.01),
        ("clip_black", 1.01),
        ("clip_white", -0.01),
        ("clip_white", 1.01),
        ("despill_amount", -0.01),
        ("despill_amount", 1.01),
        ("shrink_grow", -11),
        ("shrink_grow", 11),
        ("feather", -1),
        ("feather", 11),
    ],
)
def test_params_reject_values_outside_mandated_ranges(
    field: str, value: float
) -> None:
    with raises(KeyerError):
        KeyerParams(**{field: value})


@mark.parametrize(
    "field",
    ["tolerance", "softness", "clip_black", "clip_white", "despill_amount"],
)
@mark.parametrize("value", [nan, inf, -inf])
def test_params_reject_non_finite_values(field: str, value: float) -> None:
    with raises(KeyerError):
        KeyerParams(**{field: value})


def test_params_accept_mandated_range_boundaries() -> None:
    assert KeyerParams(
        tolerance=0.0,
        softness=1.0,
        clip_black=0.0,
        clip_white=1.0,
        shrink_grow=-10,
        feather=10,
        despill_amount=1.0,
    ).shrink_grow == -10


@mark.parametrize(
    ("field", "value", "other_values"),
    [
        ("tolerance", 0.0, {}),
        ("tolerance", 1.0, {}),
        ("softness", 0.0, {}),
        ("softness", 1.0, {}),
        ("clip_black", 0.0, {"clip_white": 1.0}),
        ("clip_white", 1.0, {"clip_black": 0.0}),
        ("despill_amount", 0.0, {}),
        ("despill_amount", 1.0, {}),
        ("shrink_grow", -10, {}),
        ("shrink_grow", 10, {}),
        ("feather", 0, {}),
        ("feather", 10, {}),
    ],
)
def test_params_accept_each_feasible_mandated_boundary(
    field: str, value: float, other_values: dict[str, float]
) -> None:
    params = KeyerParams(**other_values, **{field: value})

    assert getattr(params, field) == value


@mark.parametrize(
    ("mean_rgb", "stddev_rgb", "samples"),
    [
        ((0.0, 0.0), (0.0, 0.0, 0.0), ((0, 0, 0),)),
        ((0.0, 0.0, 0.0), (0.0, 0.0), ((0, 0, 0),)),
        ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), ()),
        ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), ((0, 0),)),
        ((0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), ((0, 0, 0),)),
        ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0), ((0, 0, 0),)),
        ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), ((0, 0, 0, 0),)),
    ],
)
def test_screen_color_model_rejects_invalid_channel_structure(
    mean_rgb: tuple[float, ...],
    stddev_rgb: tuple[float, ...],
    samples: tuple[tuple[int, ...], ...],
) -> None:
    with raises(KeyerError):
        ScreenColorModel(mean_rgb=mean_rgb, stddev_rgb=stddev_rgb, samples=samples)


@mark.parametrize(
    "sample",
    [
        (True, 0, 0),
        (-1, 0, 0),
        (256, 0, 0),
    ],
)
def test_screen_color_model_rejects_invalid_sample_channels(
    sample: tuple[int, int, int],
) -> None:
    with raises(KeyerError):
        ScreenColorModel(
            mean_rgb=(0.0, 0.0, 0.0),
            stddev_rgb=(0.0, 0.0, 0.0),
            samples=(sample,),
        )


@mark.parametrize("value", [nan, inf, -inf])
def test_screen_color_model_rejects_non_finite_statistics(value: float) -> None:
    with raises(KeyerError):
        ScreenColorModel(
            mean_rgb=(value, 0.0, 0.0),
            stddev_rgb=(0.0, 0.0, 0.0),
            samples=((0, 0, 0),),
        )


@mark.parametrize("value", [nan, inf, -inf])
def test_screen_color_model_rejects_non_finite_stddev_rgb(value: float) -> None:
    with raises(KeyerError):
        ScreenColorModel(
            mean_rgb=(0.0, 0.0, 0.0),
            stddev_rgb=(value, 0.0, 0.0),
            samples=((0, 0, 0),),
        )


def test_screen_color_model_accepts_valid_rgb_data() -> None:
    assert ScreenColorModel(
        mean_rgb=(0.1, 0.2, 0.3),
        stddev_rgb=(0.01, 0.02, 0.03),
        samples=((0, 128, 255),),
    ).samples == ((0, 128, 255),)


def test_key_image_contracts_preserve_paths() -> None:
    input_path = Path("input.png")
    output_path = Path("output.png")

    assert KeyImageOptions(input_path) == KeyImageOptions(input_path, None)
    assert KeyImageResult(output_path).output_path == output_path
