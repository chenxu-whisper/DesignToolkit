from collections.abc import Sequence
from pathlib import Path

import pytest

from designtoolkit.media_resize import (
    MediaResizeError,
    MediaResizeOptions,
    ResizeBackendResult,
    resize_media,
)


class RecordingBackend:
    def __init__(self, result: ResizeBackendResult | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.get_ffmpeg_calls = 0
        self.output_existed_before_run: list[bool] = []
        self.result = result or ResizeBackendResult(returncode=0)

    def get_ffmpeg_exe(self) -> str:
        self.get_ffmpeg_calls += 1
        return "ffmpeg"

    def run(self, command: Sequence[str]) -> ResizeBackendResult:
        self.commands.append(tuple(command))
        temporary_path = Path(command[-1])
        self.output_existed_before_run.append(temporary_path.exists())
        temporary_path.write_bytes(b"resized")
        return self.result


@pytest.mark.parametrize("size", [(1, 1), (1, 10), (10, 1)])
def test_image_resize_preserves_one_pixel_bounds(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    input_path = tmp_path / "input.png"
    input_path.write_bytes(b"image")
    backend = RecordingBackend()

    result = resize_media(
        MediaResizeOptions(input_path=input_path, size=size),
        backend,
    )

    command = backend.commands[0]
    scale = command[command.index("-vf") + 1]
    assert scale == f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease"
    assert "force_divisible_by" not in scale
    assert result.output_path == tmp_path / "input_resized.png"


@pytest.mark.parametrize("size", [(1, 10), (10, 1), (1, 1)])
def test_video_resize_rejects_sub_two_pixel_bounds_before_backend_lookup(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"video")
    backend = RecordingBackend()

    with pytest.raises(MediaResizeError, match="at least 2"):
        resize_media(MediaResizeOptions(input_path=input_path, size=size), backend)

    assert backend.get_ffmpeg_calls == 0
    assert backend.commands == []


@pytest.mark.parametrize(
    ("source_suffix", "target_suffix", "audio_codec"),
    [
        (".mkv", ".webm", "libopus"),
        (".mkv", ".mp4", "aac"),
        (".mkv", ".mov", "aac"),
        (".mkv", ".m4v", "aac"),
        (".mkv", ".avi", "libmp3lame"),
        (".webm", ".mkv", "aac"),
    ],
)
def test_cross_container_video_uses_compatible_audio(
    tmp_path: Path,
    source_suffix: str,
    target_suffix: str,
    audio_codec: str,
) -> None:
    input_path = tmp_path / f"input{source_suffix}"
    output_path = tmp_path / f"output{target_suffix}"
    input_path.write_bytes(b"video")
    backend = RecordingBackend()

    resize_media(
        MediaResizeOptions(
            input_path=input_path,
            output_path=output_path,
            size=(640, 360),
        ),
        backend,
    )

    command = backend.commands[0]
    assert command[command.index("-c:a") + 1] == audio_codec


def test_same_normalized_container_copies_audio_and_keeps_even_video_filter(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.MP4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"video")
    backend = RecordingBackend()

    resize_media(
        MediaResizeOptions(
            input_path=input_path,
            output_path=output_path,
            size=(640, 360),
        ),
        backend,
    )

    command = backend.commands[0]
    assert command[command.index("-c:a") + 1] == "copy"
    assert "force_divisible_by=2" in command[command.index("-vf") + 1]


def test_video_without_audio_maps_only_primary_video_and_optional_audio(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "silent.mp4"
    input_path.write_bytes(b"video")
    backend = RecordingBackend()

    resize_media(
        MediaResizeOptions(input_path=input_path, size=(640, 360)),
        backend,
    )

    command = backend.commands[0]
    mappings = [
        (argument, command[index + 1])
        for index, argument in enumerate(command)
        if argument == "-map"
    ]
    assert mappings == [("-map", "0:v:0"), ("-map", "0:a:0?")]


def test_failed_resize_removes_partial_temporary_output_and_preserves_stderr(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    output_path = tmp_path / "output.png"
    input_path.write_bytes(b"image")

    class PartialOutputBackend(RecordingBackend):
        def run(self, command: Sequence[str]) -> ResizeBackendResult:
            self.commands.append(tuple(command))
            Path(command[-1]).write_bytes(b"partial")
            return ResizeBackendResult(returncode=1, stderr="encode failed")

    backend = PartialOutputBackend()
    with pytest.raises(MediaResizeError, match="encode failed"):
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                output_path=output_path,
                size=(100, 100),
            ),
            backend,
        )

    assert not output_path.exists()
    temporary_path = Path(backend.commands[0][-1])
    assert temporary_path.parent.parent == output_path.parent
    assert temporary_path.suffix == output_path.suffix
    assert temporary_path != output_path
    assert not temporary_path.exists()
    assert not temporary_path.parent.exists()


def test_failed_resize_preserves_concurrently_created_final_output(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    output_path = tmp_path / "output.png"
    input_path.write_bytes(b"image")

    class ConcurrentOutputBackend(RecordingBackend):
        def run(self, command: Sequence[str]) -> ResizeBackendResult:
            self.commands.append(tuple(command))
            Path(command[-1]).write_bytes(b"partial")
            output_path.write_bytes(b"concurrent")
            return ResizeBackendResult(returncode=1, stderr="encode failed")

    backend = ConcurrentOutputBackend()
    with pytest.raises(MediaResizeError, match="encode failed"):
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                output_path=output_path,
                size=(100, 100),
            ),
            backend,
        )

    assert output_path.read_bytes() == b"concurrent"
    temporary_path = Path(backend.commands[0][-1])
    assert not temporary_path.exists()
    assert not temporary_path.parent.exists()


def test_publish_collision_preserves_concurrent_output_and_removes_temporary(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    output_path = tmp_path / "output.png"
    input_path.write_bytes(b"image")

    class ConcurrentOutputBackend(RecordingBackend):
        def run(self, command: Sequence[str]) -> ResizeBackendResult:
            self.commands.append(tuple(command))
            Path(command[-1]).write_bytes(b"complete")
            output_path.write_bytes(b"concurrent")
            return ResizeBackendResult(returncode=0)

    backend = ConcurrentOutputBackend()
    with pytest.raises(MediaResizeError, match="already exists"):
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                output_path=output_path,
                size=(100, 100),
            ),
            backend,
        )

    assert output_path.read_bytes() == b"concurrent"
    temporary_path = Path(backend.commands[0][-1])
    assert not temporary_path.exists()
    assert not temporary_path.parent.exists()


def test_success_publishes_exact_output_and_removes_temporary(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    output_path = tmp_path / "output.png"
    input_path.write_bytes(b"image")
    backend = RecordingBackend()

    result = resize_media(
        MediaResizeOptions(
            input_path=input_path,
            output_path=output_path,
            size=(100, 100),
        ),
        backend,
    )

    temporary_path = Path(backend.commands[0][-1])
    assert temporary_path.parent.parent == output_path.parent
    assert temporary_path.parent != output_path.parent
    assert temporary_path.suffix == output_path.suffix
    assert backend.output_existed_before_run == [False]
    assert output_path.read_bytes() == b"resized"
    assert not temporary_path.exists()
    assert not temporary_path.parent.exists()
    assert result.output_path == output_path
    assert result.command[-1] == str(output_path)


def test_each_resize_uses_a_different_private_output_directory(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    input_path.write_bytes(b"image")
    backends = [RecordingBackend(), RecordingBackend()]

    for index, backend in enumerate(backends):
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                output_path=tmp_path / f"output-{index}.png",
                size=(100, 100),
            ),
            backend,
        )

    temporary_paths = [Path(backend.commands[0][-1]) for backend in backends]
    assert temporary_paths[0].parent != temporary_paths[1].parent
    assert all(path.parent.parent == tmp_path for path in temporary_paths)
    assert all(not path.parent.exists() for path in temporary_paths)


@pytest.mark.parametrize("cleanup_operation", ["unlink", "rmdir"])
def test_cleanup_failure_does_not_mask_ffmpeg_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_operation: str,
) -> None:
    input_path = tmp_path / "input.png"
    output_path = tmp_path / "output.png"
    input_path.write_bytes(b"image")

    class FailedBackend(RecordingBackend):
        def run(self, command: Sequence[str]) -> ResizeBackendResult:
            self.commands.append(tuple(command))
            Path(command[-1]).write_bytes(b"partial")
            return ResizeBackendResult(returncode=1, stderr="encode failed")

    backend = FailedBackend()
    original_cleanup = getattr(Path, cleanup_operation)

    def fail_owned_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        is_owned_child = (
            cleanup_operation == "unlink" and path.parent.parent == tmp_path
        )
        is_owned_directory = cleanup_operation == "rmdir" and path.parent == tmp_path
        if is_owned_child or is_owned_directory:
            raise PermissionError(f"{cleanup_operation} denied")
        original_cleanup(path, *args, **kwargs)

    monkeypatch.setattr(Path, cleanup_operation, fail_owned_cleanup)

    with pytest.raises(MediaResizeError, match="encode failed") as error:
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                output_path=output_path,
                size=(100, 100),
            ),
            backend,
        )

    assert "FFmpeg failed" in str(error.value)
    assert any(
        f"{cleanup_operation} denied" in note
        for note in getattr(error.value, "__notes__", ())
    )

    temporary_path = Path(backend.commands[0][-1])
    monkeypatch.undo()
    temporary_path.unlink(missing_ok=True)
    temporary_path.parent.rmdir()


def test_cleanup_leaves_unexpected_foreign_files_in_private_directory(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    output_path = tmp_path / "output.png"
    input_path.write_bytes(b"image")

    class ForeignFileBackend(RecordingBackend):
        def run(self, command: Sequence[str]) -> ResizeBackendResult:
            self.commands.append(tuple(command))
            temporary_path = Path(command[-1])
            temporary_path.write_bytes(b"partial")
            (temporary_path.parent / "foreign.txt").write_bytes(b"foreign")
            return ResizeBackendResult(returncode=1, stderr="encode failed")

    backend = ForeignFileBackend()
    with pytest.raises(MediaResizeError, match="encode failed") as error:
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                output_path=output_path,
                size=(100, 100),
            ),
            backend,
        )

    temporary_path = Path(backend.commands[0][-1])
    foreign_path = temporary_path.parent / "foreign.txt"
    assert not temporary_path.exists()
    assert foreign_path.read_bytes() == b"foreign"
    assert any(
        "cleanup" in note.lower() and str(temporary_path.parent) in note
        for note in getattr(error.value, "__notes__", ())
    )

    foreign_path.unlink()
    temporary_path.parent.rmdir()


def test_existing_output_is_rejected_before_backend_and_preserved(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    output_path = tmp_path / "output.png"
    input_path.write_bytes(b"image")
    output_path.write_bytes(b"existing")
    backend = RecordingBackend()

    with pytest.raises(MediaResizeError, match="already exists"):
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                output_path=output_path,
                size=(100, 100),
            ),
            backend,
        )

    assert backend.get_ffmpeg_calls == 0
    assert backend.commands == []
    assert output_path.read_bytes() == b"existing"
