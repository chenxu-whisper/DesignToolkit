from .despill import despill
from .image_io import (
    default_output_path,
    read_rgb_image,
    validate_output_path,
    write_rgba_png,
)
from .matte import generate_alpha, refine_alpha
from .models import KeyerParams, KeyImageOptions, KeyImageResult, ScreenColorModel


def key_image(
    options: KeyImageOptions, model: ScreenColorModel, params: KeyerParams
) -> KeyImageResult:
    """Key a supported source image and write a source-sized RGBA PNG."""
    rgb = read_rgb_image(options.input_path)
    output_path = options.output_path or default_output_path(options.input_path)
    validate_output_path(options.input_path, output_path)
    alpha = refine_alpha(generate_alpha(rgb, model, params), params)
    keyed_rgb = despill(rgb, alpha, model, params.despill_amount)
    write_rgba_png(output_path, keyed_rgb, alpha)
    return KeyImageResult(output_path=output_path)
