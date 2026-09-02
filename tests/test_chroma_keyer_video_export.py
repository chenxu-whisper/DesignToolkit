from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from designtoolkit.chroma_keyer import core, video_export
from designtoolkit.chroma_keyer.models import (
    KeyerError,
    KeyerParams,
    ScreenColorModel,
)
from designtoolkit.chroma_keyer.video_export import key_video_to_png_sequence
from designtoolkit.chroma_keyer.video_models import (
    VideoKeyOptions,
    VideoMetadata,
)


def metadata() -> VideoMetadata:
    return VideoMetadata(
        width=2,
        height=1,
        fps=2.5,
        duration_seconds=1.2,
        estimated_frame_count=3,
        codec="h264",
    )


def screen_model() -> ScreenColorModel:
    return ScreenColorModel(
        mean_rgb=(0.0, 255.0, 0.0),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=((0, 255, 0),),
    )


def frames() -> tuple[np.ndarray, ...]:
    return (
        np.array([[[0, 255, 0], [255, 0, 0]]], dtype=np.uint8),
        np.array([[[0, 255, 0], [0, 0, 255]]], dtype=np.uint8),
        np.array([[[0, 255, 0], [255, 255, 255]]], dtype=np.uint8),
    )


class TrackedStream(Iterator[np.ndarray]):
    def __init__(
        self,
        source_frames: tuple[np.ndarray, ...],
        *,
        before_next: Callable[[int], None] | None = None,
        failure_at: int | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.source_frames = source_frames
        self.before_next = before_next
        self.failure_at = failure_at
        self.failure = failure or RuntimeError("decode failed")
        self.index = 0
        self.closed = False

    def __iter__(self) -> TrackedStream:
        return self

    def __next__(self) -> np.ndarray:
        if self.before_next is not None:
            self.before_next(self.index)
        if self.failure_at == self.index:
            raise self.failure
        if self.index >= len(self.source_frames):
            raise StopIteration
        frame = self.source_frames[self.index]
        self.index += 1
        return frame

    def close(self) -> None:
        self.closed = True


class FakeDecoder:
    def __init__(
        self,
        stream: TrackedStream,
        *,
        video_metadata: VideoMetadata | None = None,
        probe_error: Exception | None = None,
        iteration_error: Exception | None = None,
    ) -> None:
        self.stream = stream
        self.video_metadata = video_metadata or metadata()
        self.probe_error = probe_error
        self.iteration_error = iteration_error
        self.probe_calls: list[Path] = []
        self.iteration_calls: list[Path] = []

    def probe(self, path: Path) -> VideoMetadata:
        self.probe_calls.append(path)
        if self.probe_error is not None:
            raise self.probe_error
        return self.video_metadata

    def iter_frames(self, path: Path) -> tuple[VideoMetadata, TrackedStream]:
        self.iteration_calls.append(path)
        if self.iteration_error is not None:
            raise self.iteration_error
        return self.video_metadata, self.stream


def make_input(tmp_path: Path, suffix: str = ".mp4") -> Path:
    input_path = tmp_path / f"clip{suffix}"
    input_path.write_bytes(b"fake video")
    return input_path


def temporary_directories(root: Path, stem: str = "clip") -> list[Path]:
    return list(root.glob(f".{stem}_keyed_frames.*.tmp"))


def assert_failed_export_is_clean(root: Path, stream: TrackedStream) -> None:
    assert not (root / "clip_keyed_frames").exists()
    assert temporary_directories(root) == []
    assert stream.closed


def test_exports_frames_lazily_as_source_sized_rgba_with_manifest_and_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    output_root = tmp_path / "exports"
    output_root.mkdir()
    source_frames = frames()
    key_calls: list[np.ndarray] = []
    progress: list[tuple[int, int | None, Path]] = []
    original_key_rgb = core.key_rgb

    def assert_previous_frame_was_keyed(next_index: int) -> None:
        assert len(key_calls) == next_index

    stream = TrackedStream(
        source_frames,
        before_next=assert_previous_frame_was_keyed,
    )
    decoder = FakeDecoder(stream)
    model = screen_model()
    params = KeyerParams(
        screen_gain=1.2,
        screen_balance=0.4,
        clip_black=0.1,
        clip_white=0.9,
        shrink_grow=-1,
        feather=2,
        despill_amount=0.75,
    )

    def spy_key_rgb(
        rgb: np.ndarray,
        supplied_model: ScreenColorModel,
        supplied_params: KeyerParams,
    ) -> tuple[np.ndarray, np.ndarray]:
        key_calls.append(rgb)
        assert supplied_model is model
        assert supplied_params is params
        return original_key_rgb(rgb, supplied_model, supplied_params)

    monkeypatch.setattr(video_export.core, "key_rgb", spy_key_rgb)

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path, output_root=output_root),
        decoder,
        model,
        params,
        lambda processed, estimated, path: progress.append(
            (processed, estimated, path)
        ),
    )

    expected_dir = output_root / "clip_keyed_frames"
    assert result.output_dir == expected_dir
    assert result.manifest_path == expected_dir / "manifest.json"
    assert result.frame_count == 3
    assert result.metadata is decoder.video_metadata
    assert decoder.probe_calls == [input_path]
    assert decoder.iteration_calls == [input_path]
    assert stream.closed
    assert key_calls == list(source_frames)
    assert sorted(path.name for path in result.output_dir.glob("*.png")) == [
        "clip_keyed_000001.png",
        "clip_keyed_000002.png",
        "clip_keyed_000003.png",
    ]
    for output_path in result.output_dir.glob("*.png"):
        with Image.open(output_path) as image:
            assert image.mode == "RGBA"
            assert image.size == (2, 1)

    manifest_bytes = result.manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest_bytes.endswith(b"\n")
    assert manifest == {
        "version": 1,
        "source_path": str(input_path),
        "width": 2,
        "height": 1,
        "fps": 2.5,
        "duration_seconds": 1.2,
        "frame_count": 3,
        "pattern": "clip_keyed_%06d.png",
        "screen_color": [0, 255, 0],
        "params": {
            "screen_gain": 1.2,
            "screen_balance": 0.4,
            "clip_black": 0.1,
            "clip_white": 0.9,
            "shrink_grow": -1,
            "feather": 2,
            "despill_amount": 0.75,
        },
    }
    assert [record[:2] for record in progress] == [(1, 3), (2, 3), (3, 3)]
    assert [record[2].name for record in progress] == [
        "clip_keyed_000001.png",
        "clip_keyed_000002.png",
        "clip_keyed_000003.png",
    ]
    assert all(
        path.parent.name.startswith(".clip_keyed_frames.")
        for _, _, path in progress
    )
    assert all(path.parent.name.endswith(".tmp") for _, _, path in progress)
    assert temporary_directories(output_root) == []


def test_default_output_root_is_input_parent(tmp_path: Path) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames()[:1])

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path),
        FakeDecoder(stream),
        screen_model(),
        KeyerParams(),
    )

    assert result.output_dir == tmp_path / "clip_keyed_frames"
    assert stream.closed


@pytest.mark.parametrize(
    ("path_factory", "message"),
    [
        (lambda root: root / "missing.mp4", "Input path does not exist"),
        (lambda root: root / "directory.mp4", "Input path must be a file"),
        (lambda root: root / "clip.png", "supported video suffix"),
    ],
)
def test_input_validation_is_delegated_to_decoder_probe_and_preserves_keyer_error(
    tmp_path: Path,
    path_factory: Callable[[Path], Path],
    message: str,
) -> None:
    input_path = path_factory(tmp_path)
    if input_path.name == "directory.mp4":
        input_path.mkdir()
    elif input_path.name == "clip.png":
        input_path.write_bytes(b"not a video")
    expected_error = KeyerError(message)
    stream = TrackedStream(frames())
    decoder = FakeDecoder(stream, probe_error=expected_error)

    with pytest.raises(KeyerError) as error_info:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            decoder,
            screen_model(),
            KeyerParams(),
        )

    assert error_info.value is expected_error
    assert decoder.probe_calls == [input_path]
    assert decoder.iteration_calls == []
    assert not stream.closed
    assert temporary_directories(tmp_path) == []


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_rejects_output_root_that_is_missing_or_not_a_directory(
    tmp_path: Path,
    root_kind: str,
) -> None:
    input_path = make_input(tmp_path)
    output_root = tmp_path / "output"
    if root_kind == "file":
        output_root.write_bytes(b"not a directory")
    stream = TrackedStream(frames())
    decoder = FakeDecoder(stream)

    with pytest.raises(KeyerError, match="Output root must exist and be a directory"):
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path, output_root=output_root),
            decoder,
            screen_model(),
            KeyerParams(),
        )

    assert decoder.iteration_calls == []
    assert not stream.closed
    assert not (output_root / "clip_keyed_frames").exists()


def test_rejects_existing_final_directory_without_changing_it(
    tmp_path: Path,
) -> None:
    input_path = make_input(tmp_path)
    final_dir = tmp_path / "clip_keyed_frames"
    final_dir.mkdir()
    marker = final_dir / "existing.txt"
    marker.write_bytes(b"keep me")
    stream = TrackedStream(frames())
    decoder = FakeDecoder(stream)

    with pytest.raises(KeyerError, match="Final output directory already exists"):
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            decoder,
            screen_model(),
            KeyerParams(),
        )

    assert marker.read_bytes() == b"keep me"
    assert decoder.iteration_calls == []
    assert not stream.closed
    assert temporary_directories(tmp_path) == []


def test_decoder_failure_after_one_frame_preserves_keyer_error_and_cleans(
    tmp_path: Path,
) -> None:
    input_path = make_input(tmp_path)
    expected_error = KeyerError("decoder stopped")
    stream = TrackedStream(
        frames(),
        failure_at=1,
        failure=expected_error,
    )

    with pytest.raises(KeyerError) as error_info:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    assert error_info.value is expected_error
    assert_failed_export_is_clean(tmp_path, stream)


def test_keying_failure_cleans_temporary_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames())

    def fail_keying(*args: object) -> tuple[np.ndarray, np.ndarray]:
        raise RuntimeError("keying failed")

    monkeypatch.setattr(video_export.core, "key_rgb", fail_keying)

    with pytest.raises(KeyerError, match="Video PNG sequence export failed") as error:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    assert isinstance(error.value.__cause__, RuntimeError)
    assert_failed_export_is_clean(tmp_path, stream)


def test_png_writer_failure_cleans_all_written_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames())
    original_writer = video_export.write_rgba_png
    calls = 0

    def fail_second_write(
        output_path: Path,
        rgb: np.ndarray,
        alpha: np.ndarray,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyerError("PNG failed")
        original_writer(output_path, rgb, alpha)

    monkeypatch.setattr(video_export, "write_rgba_png", fail_second_write)

    with pytest.raises(KeyerError, match="^PNG failed$"):
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    assert_failed_export_is_clean(tmp_path, stream)


def test_progress_callback_failure_cleans_temporary_output(
    tmp_path: Path,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames())

    def fail_progress(*args: object) -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(KeyerError, match="Video PNG sequence export failed") as error:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
            fail_progress,
        )

    assert isinstance(error.value.__cause__, RuntimeError)
    assert_failed_export_is_clean(tmp_path, stream)


def test_manifest_writer_failure_cleans_temporary_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames())

    def fail_manifest(*args: object, **kwargs: object) -> None:
        raise OSError("manifest failed")

    monkeypatch.setattr(video_export.json, "dump", fail_manifest)

    with pytest.raises(KeyerError, match="Video PNG sequence export failed") as error:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    assert isinstance(error.value.__cause__, OSError)
    assert_failed_export_is_clean(tmp_path, stream)


def test_partial_final_rename_failure_removes_published_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames())
    original_replace = Path.replace

    def replace_then_fail(source: Path, target: Path) -> Path:
        original_replace(source, target)
        raise OSError("rename completion was not reported")

    monkeypatch.setattr(Path, "replace", replace_then_fail)

    with pytest.raises(KeyerError, match="Video PNG sequence export failed") as error:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    assert isinstance(error.value.__cause__, OSError)
    assert_failed_export_is_clean(tmp_path, stream)


def test_zero_decoded_frames_fails_and_cleans(tmp_path: Path) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(())

    with pytest.raises(KeyerError, match="Decoded video contains no frames"):
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    assert_failed_export_is_clean(tmp_path, stream)
