from io import BytesIO
from os import replace
from os.path import normcase
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
from PIL import Image, UnidentifiedImageError

from .models import KeyerError

_INPUT_SUFFIXES = {".png", ".jpg", ".jpeg"}


def _validate_rgb(rgb: object) -> np.ndarray:
    if (
        not isinstance(rgb, np.ndarray)
        or rgb.dtype != np.uint8
        or rgb.ndim != 3
        or rgb.shape[2] != 3
    ):
        raise KeyerError("rgb must be a uint8 HxWx3 array")
    return rgb


def _validate_alpha(alpha: object, rgb: np.ndarray) -> np.ndarray:
    if not isinstance(alpha, np.ndarray) or alpha.dtype != np.uint8 or alpha.ndim != 2:
        raise KeyerError("alpha must be a uint8 HxW array")
    if alpha.shape != rgb.shape[:2]:
        raise KeyerError("alpha dimensions must match rgb")
    return alpha


def _validate_preview_limit(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KeyerError(f"{name} must be a positive integer")
    return value


def _resolved_path(path: Path) -> str:
    try:
        return normcase(str(path.resolve(strict=False)))
    except OSError:
        return normcase(str(path.absolute()))


def _validate_png_destination(output_path: Path) -> None:
    if output_path.suffix.lower() != ".png":
        raise KeyerError("Output path must have a .png suffix")
    if not output_path.parent.exists() or not output_path.parent.is_dir():
        raise KeyerError("Output parent directory must exist")
    if output_path.exists():
        raise KeyerError("Output path already exists")


def read_rgb_image(path: Path) -> np.ndarray:
    """Read a supported image source and normalize it to RGB uint8 pixels."""
    if not path.exists():
        raise KeyerError("Input path does not exist")
    if not path.is_file():
        raise KeyerError("Input path must be a file")
    if path.suffix.lower() not in _INPUT_SUFFIXES:
        raise KeyerError("Input path must have a supported image suffix")

    try:
        with Image.open(path) as image:
            return np.asarray(image.convert("RGB"), dtype=np.uint8)
    except (OSError, UnidentifiedImageError) as error:
        raise KeyerError("Input image could not be read") from error


def default_output_path(input_path: Path) -> Path:
    """Return the default keyed PNG path beside an input image."""
    return input_path.with_name(f"{input_path.stem}_keyed.png")


def validate_output_path(input_path: Path, output_path: Path) -> None:
    """Validate a new PNG destination distinct from the source image."""
    _validate_png_destination(output_path)
    if _resolved_path(input_path) == _resolved_path(output_path):
        raise KeyerError("Output path must differ from input path")


def write_rgba_png(output_path: Path, rgb: np.ndarray, alpha: np.ndarray) -> None:
    """Atomically write validated RGB pixels and alpha matte as a PNG."""
    rgb = _validate_rgb(rgb)
    alpha = _validate_alpha(alpha, rgb)
    _validate_png_destination(output_path)
    rgba = np.dstack((rgb, alpha))
    temporary_path: Path | None = None

    try:
        with NamedTemporaryFile(
            mode="wb", suffix=".png", dir=output_path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            Image.fromarray(rgba, mode="RGBA").save(temporary, format="PNG")
        replace(temporary_path, output_path)
    except OSError as error:
        raise KeyerError("Output image could not be written") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def make_preview_png(
    rgb: np.ndarray, max_width: int = 1280, max_height: int = 720
) -> bytes:
    """Encode a bounded, aspect-preserving RGB PNG preview."""
    rgb = _validate_rgb(rgb)
    max_width = _validate_preview_limit("max_width", max_width)
    max_height = _validate_preview_limit("max_height", max_height)
    height, width = rgb.shape[:2]
    scale = min(max_width / width, max_height / height, 1.0)
    target_size = (max(1, int(width * scale)), max(1, int(height * scale)))

    image = Image.fromarray(rgb, mode="RGB")
    if target_size != image.size:
        image = image.resize(target_size, Image.Resampling.LANCZOS)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
