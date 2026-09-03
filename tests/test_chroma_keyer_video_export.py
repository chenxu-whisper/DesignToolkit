from __future__ import annotations

import errno
import gc
import json
import os
import weakref
from collections.abc import Callable, Iterator
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize(
    ("samples", "mean_rgb", "message"),
    [
        pytest.param((), (0.0, 255.0, 0.0), "samples must not be empty", id="zero"),
        pytest.param(
            ((0, 255, 0), (0, 0, 255)),
            (0.0, 255.0, 0.0),
            "samples must contain exactly one screen color",
            id="multiple",
        ),
        pytest.param(
            ((128, 128, 128),),
            (128.0, 128.0, 128.0),
            "请选择具有明显主色的纯色幕背景",
            id="invalid-sample",
        ),
        pytest.param(
            ((0, 255, 0),),
            (0.0, 254.0, 0.0),
            "mean_rgb must match the sole screen color sample",
            id="inconsistent-mean",
        ),
    ],
)
def test_rejects_invalid_screen_color_model_before_probe_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    samples: tuple[tuple[int, int, int], ...],
    mean_rgb: tuple[float, float, float],
    message: str,
) -> None:
    input_path = make_input(tmp_path)
    output_root = tmp_path / "exports"
    output_root.mkdir()
    stream = TrackedStream(frames())
    decoder = FakeDecoder(stream)
    model = screen_model()
    object.__setattr__(model, "samples", samples)
    object.__setattr__(model, "mean_rgb", mean_rgb)

    def fail_path_query(path: Path, *args: object, **kwargs: object) -> None:
        pytest.fail(f"filesystem queried before model validation: {path}")

    with monkeypatch.context() as path_queries:
        for method_name in ("exists", "is_dir", "is_file", "stat"):
            path_queries.setattr(Path, method_name, fail_path_query)

        with pytest.raises(KeyerError, match=f"^{message}$"):
            key_video_to_png_sequence(
                VideoKeyOptions(input_path=input_path, output_root=output_root),
                decoder,
                model,
                KeyerParams(),
            )

    assert decoder.probe_calls == []
    assert decoder.iteration_calls == []
    assert list(output_root.iterdir()) == []
    assert not (output_root / "clip_keyed_frames").exists()
    assert temporary_directories(output_root) == []
    assert not stream.closed


def test_accepts_float_normalization_noise_in_screen_color_mean(
    tmp_path: Path,
) -> None:
    input_path = make_input(tmp_path)
    model = screen_model()
    object.__setattr__(
        model,
        "mean_rgb",
        tuple(np.nextafter(channel, np.inf) for channel in model.mean_rgb),
    )
    decoder = FakeDecoder(TrackedStream(frames()[:1]))

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path),
        decoder,
        model,
        KeyerParams(),
    )

    assert result.frame_count == 1
    assert decoder.probe_calls == [input_path]


class FakeRenameAt2:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: tuple[object, ...] | None = None
        self.restype: object | None = None

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


def patch_linux_publication_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    libc: object,
    *,
    error_number: int = 0,
) -> list[tuple[object, bool]]:
    load_calls: list[tuple[object, bool]] = []

    def load_library(name: object, *, use_errno: bool) -> object:
        load_calls.append((name, use_errno))
        return libc

    monkeypatch.setattr(video_export.os, "name", "posix")
    monkeypatch.setattr(video_export.sys, "platform", "linux")
    monkeypatch.setattr(video_export.ctypes, "CDLL", load_library)
    monkeypatch.setattr(video_export.ctypes, "get_errno", lambda: error_number)
    return load_calls


def test_publish_directory_no_replace_uses_linux_renameat2_no_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("staging")
    destination = Path("published")
    renameat2 = FakeRenameAt2(result=0)
    load_calls = patch_linux_publication_dependencies(
        monkeypatch,
        SimpleNamespace(renameat2=renameat2),
    )

    video_export._publish_directory_no_replace(source, destination)

    assert load_calls == [(None, True)]
    assert renameat2.argtypes == (
        video_export.ctypes.c_int,
        video_export.ctypes.c_char_p,
        video_export.ctypes.c_int,
        video_export.ctypes.c_char_p,
        video_export.ctypes.c_uint,
    )
    assert renameat2.restype is video_export.ctypes.c_int
    assert renameat2.calls == [
        (
            video_export._AT_FDCWD,
            os.fsencode(source),
            video_export._AT_FDCWD,
            os.fsencode(destination),
            video_export._RENAME_NOREPLACE,
        )
    ]


def test_publish_directory_no_replace_propagates_linux_eexist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("staging")
    destination = Path("published")
    renameat2 = FakeRenameAt2(result=-1)
    patch_linux_publication_dependencies(
        monkeypatch,
        SimpleNamespace(renameat2=renameat2),
        error_number=errno.EEXIST,
    )

    with pytest.raises(FileExistsError) as error:
        video_export._publish_directory_no_replace(source, destination)

    assert error.value.errno == errno.EEXIST
    assert error.value.filename == destination


@pytest.mark.parametrize(
    "error_number",
    sorted(
        {
            errno.EINVAL,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }
    ),
    ids=lambda error_number: errno.errorcode[error_number],
)
def test_publish_directory_no_replace_fails_closed_for_unsupported_linux_errno(
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    source = Path("staging")
    destination = Path("published")
    renameat2 = FakeRenameAt2(result=-1)
    patch_linux_publication_dependencies(
        monkeypatch,
        SimpleNamespace(renameat2=renameat2),
        error_number=error_number,
    )

    with pytest.raises(
        KeyerError,
        match=(
            "Atomic no-replace directory publication is unavailable: "
            "renameat2 RENAME_NOREPLACE is unsupported"
        ),
    ) as error:
        video_export._publish_directory_no_replace(source, destination)

    assert isinstance(error.value.__cause__, OSError)
    assert error.value.__cause__.errno == error_number
    assert error.value.__cause__.filename == destination


def test_publish_directory_no_replace_propagates_unexpected_linux_errno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("staging")
    destination = Path("published")
    renameat2 = FakeRenameAt2(result=-1)
    patch_linux_publication_dependencies(
        monkeypatch,
        SimpleNamespace(renameat2=renameat2),
        error_number=errno.EACCES,
    )

    with pytest.raises(OSError) as error:
        video_export._publish_directory_no_replace(source, destination)

    assert error.value.errno == errno.EACCES
    assert error.value.filename == destination


def test_publish_directory_no_replace_fails_closed_without_linux_renameat2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("staging")
    destination = Path("published")
    patch_linux_publication_dependencies(monkeypatch, SimpleNamespace())

    with pytest.raises(
        KeyerError,
        match=(
            "Atomic no-replace directory publication is unavailable: "
            "libc renameat2 is missing"
        ),
    ) as error:
        video_export._publish_directory_no_replace(source, destination)

    assert isinstance(error.value.__cause__, AttributeError)


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


def test_manifest_serializes_accepted_non_builtin_parameter_numbers(
    tmp_path: Path,
) -> None:
    input_path = make_input(tmp_path)
    params = KeyerParams(
        screen_gain=np.float32(1.25),
        screen_balance=Fraction(1, 3),
        clip_black=np.float32(0.125),
        clip_white=Fraction(7, 8),
        shrink_grow=-2,
        feather=3,
        despill_amount=Fraction(3, 4),
    )

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path),
        FakeDecoder(TrackedStream(frames()[:1])),
        screen_model(),
        params,
    )

    manifest_params = json.loads(result.manifest_path.read_bytes())["params"]
    assert manifest_params == {
        "screen_gain": float(params.screen_gain),
        "screen_balance": float(params.screen_balance),
        "clip_black": float(params.clip_black),
        "clip_white": float(params.clip_white),
        "shrink_grow": int(params.shrink_grow),
        "feather": int(params.feather),
        "despill_amount": float(params.despill_amount),
    }


def test_releases_frame_arrays_before_requesting_the_next_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    array_references: list[weakref.ReferenceType[np.ndarray]] = []
    original_key_rgb = core.key_rgb

    class CollectibleStream(Iterator[np.ndarray]):
        def __init__(self) -> None:
            self.index = 0
            self.closed = False

        def __iter__(self) -> CollectibleStream:
            return self

        def __next__(self) -> np.ndarray:
            if self.index:
                gc.collect()
                assert all(reference() is None for reference in array_references)
                array_references.clear()
            if self.index == 2:
                raise StopIteration
            frame = np.full((1, 2, 3), self.index, dtype=np.uint8)
            self.index += 1
            return frame

        def close(self) -> None:
            self.closed = True

    def track_arrays(
        rgb: np.ndarray,
        model: ScreenColorModel,
        params: KeyerParams,
    ) -> tuple[np.ndarray, np.ndarray]:
        keyed_rgb, alpha = original_key_rgb(rgb, model, params)
        array_references.extend(
            (weakref.ref(rgb), weakref.ref(keyed_rgb), weakref.ref(alpha))
        )
        return keyed_rgb, alpha

    stream = CollectibleStream()
    monkeypatch.setattr(video_export.core, "key_rgb", track_arrays)

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path),
        FakeDecoder(stream),  # type: ignore[arg-type]
        screen_model(),
        KeyerParams(),
    )

    assert result.frame_count == 2
    assert stream.closed


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


def test_success_does_not_publish_ownership_marker(tmp_path: Path) -> None:
    input_path = make_input(tmp_path)

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path),
        FakeDecoder(TrackedStream(frames()[:1])),
        screen_model(),
        KeyerParams(),
    )

    assert not (
        result.output_dir / ".designtoolkit-video-export-owner"
    ).exists()


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


def test_rejects_existing_final_file_without_changing_it(tmp_path: Path) -> None:
    input_path = make_input(tmp_path)
    final_path = tmp_path / "clip_keyed_frames"
    final_path.write_bytes(b"keep me")
    stream = TrackedStream(frames())
    decoder = FakeDecoder(stream)

    with pytest.raises(KeyerError, match="Final output directory already exists"):
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            decoder,
            screen_model(),
            KeyerParams(),
        )

    assert final_path.read_bytes() == b"keep me"
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


@pytest.mark.parametrize("transient_failures", [1, 2])
def test_cleanup_retries_transient_permission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transient_failures: int,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames())
    original_rmtree = video_export.rmtree
    cleanup_attempts: list[Path] = []
    sleep_delays: list[float] = []

    def flaky_rmtree(path: Path, *args: object, **kwargs: object) -> None:
        cleanup_attempts.append(path)
        if len(cleanup_attempts) <= transient_failures:
            raise PermissionError("directory is busy")
        original_rmtree(path, *args, **kwargs)

    def fail_progress(*args: object) -> None:
        raise RuntimeError("callback failed")

    monkeypatch.setattr(video_export, "rmtree", flaky_rmtree)
    monkeypatch.setattr(video_export, "sleep", sleep_delays.append, raising=False)

    with pytest.raises(KeyerError, match="Video PNG sequence export failed") as error:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
            fail_progress,
        )

    assert isinstance(error.value.__cause__, RuntimeError)
    assert len(cleanup_attempts) == transient_failures + 1
    assert sleep_delays == [0.01, 0.02][:transient_failures]
    assert_failed_export_is_clean(tmp_path, stream)


def test_cleanup_exhaustion_raises_path_specific_keyer_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames())
    cleanup_attempts: list[Path] = []
    sleep_delays: list[float] = []

    def fail_rmtree(path: Path, *args: object, **kwargs: object) -> None:
        cleanup_attempts.append(path)
        raise PermissionError("directory remains busy")

    def fail_progress(*args: object) -> None:
        raise RuntimeError("callback failed")

    monkeypatch.setattr(video_export, "rmtree", fail_rmtree)
    monkeypatch.setattr(video_export, "sleep", sleep_delays.append, raising=False)

    with pytest.raises(KeyerError, match="Failed to clean export directory") as error:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
            fail_progress,
        )

    assert len(cleanup_attempts) == 3
    assert sleep_delays == [0.01, 0.02]
    assert str(cleanup_attempts[0]) in str(error.value)
    assert isinstance(error.value.__cause__, PermissionError)
    assert isinstance(error.value.__context__, KeyerError)
    assert isinstance(error.value.__context__.__cause__, RuntimeError)
    assert temporary_directories(tmp_path) == [cleanup_attempts[0]]
    assert stream.closed


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


def test_publish_failure_cleans_temporary_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames())
    temporary_dir: Path | None = None

    def fail_publish(source: Path, _target: Path) -> None:
        nonlocal temporary_dir
        temporary_dir = source
        raise OSError("publish failed")

    monkeypatch.setattr(
        video_export,
        "_publish_directory_no_replace",
        fail_publish,
    )

    with pytest.raises(KeyerError, match="Video PNG sequence export failed") as error:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    assert isinstance(error.value.__cause__, OSError)
    assert temporary_dir is not None
    assert not temporary_dir.exists()
    assert_failed_export_is_clean(tmp_path, stream)


def test_successful_publish_never_touches_replaced_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames()[:1])
    final_dir = tmp_path / "clip_keyed_frames"
    displaced_dir = tmp_path / "displaced-export"
    foreign_dir = tmp_path / "foreign-directory"
    foreign_file = foreign_dir / "unrelated.txt"
    foreign_dir.mkdir()
    foreign_file.write_text("keep me", encoding="utf-8")
    foreign_identity = foreign_dir.stat()
    original_publish = video_export._publish_directory_no_replace

    def publish_then_replace_destination(
        source: Path,
        destination: Path,
    ) -> None:
        original_publish(source, destination)
        destination.rename(displaced_dir)
        foreign_dir.rename(destination)
        video_export.rmtree(displaced_dir)

    monkeypatch.setattr(
        video_export,
        "_publish_directory_no_replace",
        publish_then_replace_destination,
    )

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path),
        FakeDecoder(stream),
        screen_model(),
        KeyerParams(),
    )

    final_identity = final_dir.stat()
    assert result.output_dir == final_dir
    assert result.manifest_path == final_dir / "manifest.json"
    assert (final_identity.st_dev, final_identity.st_ino) == (
        foreign_identity.st_dev,
        foreign_identity.st_ino,
    )
    final_foreign_file = final_dir / foreign_file.name
    assert final_foreign_file.read_text(encoding="utf-8") == "keep me"
    assert list(final_dir.iterdir()) == [final_foreign_file]
    assert temporary_directories(tmp_path) == []
    assert stream.closed


def test_result_is_prepared_before_successful_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    expected_result = object()
    events: list[str] = []
    original_publish = video_export._publish_directory_no_replace

    def build_result(**_kwargs: object) -> object:
        events.append("result")
        return expected_result

    def publish(source: Path, destination: Path) -> None:
        events.append("publish")
        original_publish(source, destination)

    monkeypatch.setattr(video_export, "VideoKeyResult", build_result)
    monkeypatch.setattr(video_export, "_publish_directory_no_replace", publish)

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path),
        FakeDecoder(TrackedStream(frames()[:1])),
        screen_model(),
        KeyerParams(),
    )

    assert result is expected_result
    assert events == ["result", "publish"]


def test_publish_failure_does_not_remove_foreign_final_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames())

    def publish_with_unowned_final(source: Path, target: Path) -> None:
        video_export.rmtree(source)
        target.mkdir()
        (target / "unrelated.txt").write_text("keep me", encoding="utf-8")
        raise OSError("rename raced with another publisher")

    monkeypatch.setattr(
        video_export,
        "_publish_directory_no_replace",
        publish_with_unowned_final,
    )

    with pytest.raises(KeyerError, match="Video PNG sequence export failed") as error:
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    final_dir = tmp_path / "clip_keyed_frames"
    assert isinstance(error.value.__cause__, OSError)
    assert (final_dir / "unrelated.txt").read_text(encoding="utf-8") == "keep me"
    assert temporary_directories(tmp_path) == []
    assert stream.closed


def test_publish_race_does_not_replace_empty_destination_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames()[:1])
    final_dir = tmp_path / "clip_keyed_frames"
    original_publish = video_export._publish_directory_no_replace
    destination_identity: tuple[int, int] | None = None

    def publish_after_competing_directory(
        source: Path,
        destination: Path,
    ) -> None:
        nonlocal destination_identity
        destination.mkdir()
        stat = destination.stat()
        destination_identity = (stat.st_dev, stat.st_ino)
        original_publish(source, destination)

    monkeypatch.setattr(
        video_export,
        "_publish_directory_no_replace",
        publish_after_competing_directory,
    )

    with pytest.raises(KeyerError, match="Video PNG sequence export failed"):
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    stat = final_dir.stat()
    assert (stat.st_dev, stat.st_ino) == destination_identity
    assert list(final_dir.iterdir()) == []
    assert temporary_directories(tmp_path) == []
    assert stream.closed


def test_publish_race_does_not_replace_dangling_destination_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = make_input(tmp_path)
    stream = TrackedStream(frames()[:1])
    final_dir = tmp_path / "clip_keyed_frames"
    dangling_target = tmp_path / "missing-target"
    original_publish = video_export._publish_directory_no_replace

    def publish_after_competing_symlink(
        source: Path,
        destination: Path,
    ) -> None:
        try:
            destination.symlink_to(dangling_target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks are unavailable: {error}")
        original_publish(source, destination)

    monkeypatch.setattr(
        video_export,
        "_publish_directory_no_replace",
        publish_after_competing_symlink,
    )

    with pytest.raises(KeyerError, match="Video PNG sequence export failed"):
        key_video_to_png_sequence(
            VideoKeyOptions(input_path=input_path),
            FakeDecoder(stream),
            screen_model(),
            KeyerParams(),
        )

    assert final_dir.is_symlink()
    assert final_dir.readlink() == dangling_target
    assert temporary_directories(tmp_path) == []
    assert stream.closed


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
