"""Output validation and offline player publication for packed video tasks."""

from collections.abc import Callable
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

from designtoolkit.chroma_keyer.models import KeyerError
from designtoolkit.chroma_keyer.video_export import _publish_directory_no_replace
from designtoolkit.chroma_keyer.video_models import VideoKeyOptions
from designtoolkit.output_names import bounded_derived_name
from designtoolkit.web.schemas import ChromaVideoKeyerTaskRequest


def hosted_video_request(
    request: ChromaVideoKeyerTaskRequest, input_path: Path, task_dir: Path
) -> ChromaVideoKeyerTaskRequest:
    output = (
        task_dir
        / bounded_derived_name(
            prefix="", stem=input_path.stem, suffix="_keyed_packed", extension=".mp4"
        )
        if request.output_format == "packed_alpha_mp4"
        else None
    )
    return request.model_copy(
        update={
            "input_path": str(input_path),
            "output_root": str(task_dir),
            "output_path": str(output) if output is not None else None,
        }
    )


def packed_output_path(options: VideoKeyOptions) -> Path:
    root = options.output_root or options.input_path.parent
    output = options.output_path or root / bounded_derived_name(
        prefix="",
        stem=options.input_path.stem,
        suffix="_keyed_packed",
        extension=".mp4",
    )
    if not output.parent.is_dir():
        raise KeyerError("Output root must exist and be a directory")
    if output.suffix.lower() != ".mp4":
        raise KeyerError("Packed video output must end with .mp4")
    if output.resolve() == options.input_path.resolve():
        raise KeyerError("Output cannot replace input video")
    for path in (output, output.with_suffix(".json"), player_output_path(output)):
        if path.exists() or path.is_symlink():
            raise KeyerError(f"Output already exists: {path.name}")
    return output


def player_output_path(output: Path) -> Path:
    return output.parent / bounded_derived_name(
        prefix="", stem=output.stem, suffix="_player"
    )


def publish_player(output: Path, output_check: Callable[[], None]) -> Path:
    destination = player_output_path(output)
    source = Path(__file__).parents[1] / "chroma_keyer" / "player"
    temporary = destination.parent / f".player.{uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        for name in ("index.html", "player.js", "player.css", "README.md"):
            with (temporary / name).open("xb") as target:
                target.write((source / name).read_bytes())
            output_check()
        _publish_directory_no_replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            rmtree(temporary)
