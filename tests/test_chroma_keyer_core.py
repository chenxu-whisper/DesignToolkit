from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image
from pytest import raises

from designtoolkit.chroma_keyer import core as chroma_keyer_core
from designtoolkit.chroma_keyer import image_io
from designtoolkit.chroma_keyer.core import key_image
from designtoolkit.chroma_keyer.image_io import (
    default_output_path,
    make_preview_png,
    read_rgb_image,
    validate_output_path,
    write_rgba_png,
)
from designtoolkit.chroma_keyer.models import (
    KeyerError,
    KeyerParams,
    KeyImageOptions,
    ScreenColorModel,
)


def _save_rgb(path: Path, pixels: np.ndarray) -> None:
    Image.fromarray(pixels, mode="RGB").save(path)


def _green_screen_model() -> ScreenColorModel:
    return ScreenColorModel(
        mean_rgb=(42.0, 180.0, 72.0),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((42, 180, 72),),
    )


def test_key_rgb_returns_refined_alpha_and_despilled_rgb() -> None:
    source_rgb = np.array(
        [[[0, 255, 0], [180, 235, 180], [255, 0, 0]]],
        dtype=np.uint8,
    )
    params = KeyerParams(tolerance=0.0, softness=1.0, despill_amount=1.0)
    model = ScreenColorModel(
        mean_rgb=(0.0, 255.0, 0.0),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((0, 255, 0),),
    )

    assert hasattr(chroma_keyer_core, "key_rgb")
    keyed_rgb, alpha = chroma_keyer_core.key_rgb(source_rgb, model, params)

    assert keyed_rgb.dtype == np.uint8
    assert np.array_equal(keyed_rgb[0, 0], source_rgb[0, 0])
    assert keyed_rgb[0, 1, 1] < source_rgb[0, 1, 1]
    assert alpha.tolist() == [[0, alpha[0, 1], 255]]
    assert 0 < alpha[0, 1] < 255


def test_key_image_writes_source_sized_rgba_png(tmp_path: Path) -> None:
    input_path = tmp_path / "screen.png"
    output_path = tmp_path / "result.png"
    _save_rgb(
        input_path,
        np.array([[[42, 180, 72], [220, 40, 40]]], dtype=np.uint8),
    )

    result = key_image(
        KeyImageOptions(input_path=input_path, output_path=output_path),
        _green_screen_model(),
        KeyerParams(softness=0.1),
    )

    assert result.output_path == output_path
    with Image.open(output_path) as image:
        assert image.mode == "RGBA"
        assert image.size == (2, 1)
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((1, 0))[3] > 0


def test_key_image_preserves_fractional_edge_alpha_and_despills_green(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    output_path = tmp_path / "result.png"
    _save_rgb(
        input_path,
        np.array([[[0, 255, 0], [180, 235, 180], [255, 0, 0]]], dtype=np.uint8),
    )
    model = ScreenColorModel(
        mean_rgb=(0.0, 255.0, 0.0),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((0, 255, 0),),
    )

    key_image(
        KeyImageOptions(input_path=input_path, output_path=output_path),
        model,
        KeyerParams(tolerance=0.0, softness=1.0, despill_amount=1.0),
    )

    with Image.open(output_path) as image:
        pixels = np.asarray(image.convert("RGBA"))

    assert pixels[0, 0, 3] == 0
    assert 0 < pixels[0, 1, 3] < 255
    assert pixels[0, 1, 1] < 235
    assert pixels[0, 2, 3] == 255


def test_default_output_path_and_key_image_omitted_output_use_keyed_suffix(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    _save_rgb(input_path, np.array([[[42, 180, 72]]], dtype=np.uint8))

    result = key_image(
        KeyImageOptions(input_path=input_path),
        _green_screen_model(),
        KeyerParams(),
    )

    assert default_output_path(input_path) == tmp_path / "screen_keyed.png"
    assert result.output_path == tmp_path / "screen_keyed.png"
    assert result.output_path.is_file()


def test_existing_output_is_refused_without_changing_its_bytes(tmp_path: Path) -> None:
    input_path = tmp_path / "screen.png"
    output_path = tmp_path / "result.png"
    _save_rgb(input_path, np.array([[[42, 180, 72]]], dtype=np.uint8))
    original = b"existing image bytes"
    output_path.write_bytes(original)

    with raises(KeyerError, match="^Output path already exists$"):
        key_image(
            KeyImageOptions(input_path=input_path, output_path=output_path),
            _green_screen_model(),
            KeyerParams(),
        )

    assert output_path.read_bytes() == original


def test_read_rgb_image_rejects_unsupported_and_normalizes_rgba(tmp_path: Path) -> None:
    gif_path = tmp_path / "screen.gif"
    Image.new("RGB", (1, 1), (42, 180, 72)).save(gif_path, format="GIF")
    rgba_path = tmp_path / "source.png"
    Image.new("RGBA", (1, 1), (12, 34, 56, 78)).save(rgba_path)

    with raises(KeyerError):
        read_rgb_image(gif_path)

    rgb = read_rgb_image(rgba_path)

    assert rgb.dtype == np.uint8
    assert rgb.shape == (1, 1, 3)
    assert rgb.tolist() == [[[12, 34, 56]]]


def test_read_rgb_image_accepts_supported_suffixes_case_insensitively(
    tmp_path: Path,
) -> None:
    for filename, image_format in (
        ("source.png", "PNG"),
        ("source.jpg", "JPEG"),
        ("source.jpeg", "JPEG"),
        ("upper.PNG", "PNG"),
        ("upper.JPG", "JPEG"),
        ("upper.JPEG", "JPEG"),
    ):
        path = tmp_path / filename
        Image.new("RGB", (1, 1), (12, 34, 56)).save(path, format=image_format)

        rgb = read_rgb_image(path)

        assert rgb.dtype == np.uint8
        assert rgb.shape == (1, 1, 3)


def test_read_rgb_image_rejects_missing_directory_corrupt_and_gif_paths(
    tmp_path: Path,
) -> None:
    directory_path = tmp_path / "directory.png"
    directory_path.mkdir()
    corrupt_path = tmp_path / "corrupt.JPG"
    corrupt_path.write_bytes(b"not an image")
    gif_path = tmp_path / "screen.gif"
    Image.new("RGB", (1, 1), (42, 180, 72)).save(gif_path, format="GIF")

    for path in (
        tmp_path / "missing.png",
        directory_path,
        corrupt_path,
        gif_path,
    ):
        with raises(KeyerError):
            read_rgb_image(path)


def test_output_path_must_be_new_png_in_an_existing_parent(tmp_path: Path) -> None:
    input_path = tmp_path / "screen.png"
    _save_rgb(input_path, np.array([[[42, 180, 72]]], dtype=np.uint8))

    with raises(KeyerError):
        validate_output_path(input_path, tmp_path / "result.jpg")
    with raises(KeyerError):
        validate_output_path(input_path, input_path)
    with raises(KeyerError):
        validate_output_path(input_path, tmp_path / "missing" / "result.png")


def test_make_preview_png_constrains_size_preserves_aspect_and_never_upscales() -> None:
    rgb = np.zeros((100, 400, 3), dtype=np.uint8)

    preview = make_preview_png(rgb, max_width=100, max_height=100)
    unchanged = make_preview_png(rgb, max_width=800, max_height=800)

    with Image.open(BytesIO(preview)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (100, 25)
    with Image.open(BytesIO(unchanged)) as image:
        assert image.size == (400, 100)


def test_make_preview_png_resizes_and_preserves_alpha_from_rgb_or_rgba() -> None:
    rgb = np.zeros((100, 400, 3), dtype=np.uint8)
    alpha = np.zeros((100, 400), dtype=np.uint8)
    alpha[:, 200:] = 255
    rgba = np.dstack((rgb, alpha))

    for pixels, supplied_alpha in ((rgb, alpha), (rgba, None)):
        preview = make_preview_png(
            pixels,
            alpha=supplied_alpha,
            max_width=100,
            max_height=100,
        )

        with Image.open(BytesIO(preview)) as image:
            assert image.format == "PNG"
            assert image.mode == "RGBA"
            assert image.size == (100, 25)
            assert image.getpixel((0, 0))[3] == 0
            assert image.getpixel((99, 0))[3] == 255


def test_make_preview_png_rejects_invalid_limits_and_rgb() -> None:
    rgb = np.zeros((2, 2, 3), dtype=np.uint8)

    with raises(KeyerError):
        make_preview_png(rgb, max_width=True)
    with raises(KeyerError):
        make_preview_png(rgb, max_height=0)
    with raises(KeyerError):
        make_preview_png(np.zeros((2, 2, 3), dtype=np.float32))
    with raises(KeyerError):
        make_preview_png(rgb, alpha=np.zeros((3, 2), dtype=np.uint8))


def test_make_preview_png_rejects_empty_rgb_dimensions() -> None:
    for rgb in (
        np.zeros((0, 1, 3), dtype=np.uint8),
        np.zeros((1, 0, 3), dtype=np.uint8),
    ):
        with raises(KeyerError, match="^rgb must be a uint8 HxWx3 array$"):
            make_preview_png(rgb)


def test_write_rgba_png_rejects_bad_rgb_or_mismatched_alpha(tmp_path: Path) -> None:
    output_path = tmp_path / "result.png"
    rgb = np.zeros((2, 2, 3), dtype=np.uint8)

    with raises(KeyerError):
        write_rgba_png(output_path, rgb.astype(np.float32), np.zeros((2, 2), np.uint8))
    with raises(KeyerError):
        write_rgba_png(output_path, rgb, np.zeros((3, 2), np.uint8))


def test_write_rgba_png_rejects_empty_rgb_dimensions(tmp_path: Path) -> None:
    for rgb, alpha in (
        (np.zeros((0, 1, 3), dtype=np.uint8), np.zeros((0, 1), dtype=np.uint8)),
        (np.zeros((1, 0, 3), dtype=np.uint8), np.zeros((1, 0), dtype=np.uint8)),
    ):
        with raises(KeyerError, match="^rgb must be a uint8 HxWx3 array$"):
            write_rgba_png(tmp_path / "result.png", rgb, alpha)


def test_write_rgba_png_rejects_existing_output_and_accepts_uppercase_png(
    tmp_path: Path,
) -> None:
    rgb = np.zeros((1, 1, 3), dtype=np.uint8)
    alpha = np.full((1, 1), 255, dtype=np.uint8)
    existing_path = tmp_path / "existing.png"
    existing_path.write_bytes(b"existing")

    with raises(KeyerError, match="^Output path already exists$"):
        write_rgba_png(existing_path, rgb, alpha)

    output_path = tmp_path / "result.PNG"
    write_rgba_png(output_path, rgb, alpha)

    assert output_path.is_file()
    with Image.open(output_path) as image:
        assert image.mode == "RGBA"


def test_write_rgba_png_preserves_racing_destination_and_removes_temporary_file(
    tmp_path: Path, monkeypatch
) -> None:
    output_path = tmp_path / "result.png"
    rgb = np.zeros((1, 1, 3), dtype=np.uint8)
    alpha = np.full((1, 1), 255, dtype=np.uint8)
    racing_bytes = b"racing destination bytes"
    original_link = image_io.link

    def create_destination_before_link(source: Path, destination: Path) -> None:
        assert source.is_file()
        assert destination == output_path
        destination.write_bytes(racing_bytes)
        original_link(source, destination)

    monkeypatch.setattr(image_io, "link", create_destination_before_link)

    with raises(KeyerError, match="^Output path already exists$"):
        write_rgba_png(output_path, rgb, alpha)

    assert output_path.read_bytes() == racing_bytes
    assert not list(tmp_path.glob("tmp*.png"))
