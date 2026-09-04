from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fakeredis
import pytest
from rq import Queue

from designtoolkit.chroma_keyer.models import KeyerError, KeyImageResult
from designtoolkit.media_resize import (
    MediaResizeResult,
    ResizeBackendResult,
    resize_media,
)
from designtoolkit.video_frames import ExportResult
from designtoolkit.web.adapters import media_resize_options_list_from_request
from designtoolkit.web.redis_tasks import RedisLogStore, RedisTaskManager
from designtoolkit.web.schemas import (
    ChromaKeyerTaskRequest,
    ChromaVideoKeyerTaskRequest,
    LogLevel,
    LogPhase,
    MediaResizeTaskRequest,
    TaskKind,
    TaskStatus,
    VideoFramesTaskRequest,
)
from designtoolkit.web.tasks import TaskConflictError
from designtoolkit.web.worker import RedisTaskExecutor


def _manager(
    redis_connection,
    tmp_path: Path,
    *,
    max_queued_tasks: int = 20,
) -> RedisTaskManager:
    return RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=max_queued_tasks,
        retention_seconds=7200,
    )


def test_redis_manager_enqueues_and_isolates_task_records(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)

    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    assert task.status is TaskStatus.QUEUED
    assert manager.get_task(task.task_id, owner_id="a" * 32) == task
    assert manager.get_task(task.task_id, owner_id="b" * 32) is None
    assert manager.queue.count == 1
    assert redis_connection.ttl(f"dt:task:{task.task_id}") >= 24 * 60 * 60 - 1
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    assert job.func_name == "designtoolkit.web.worker.run_task"


def test_redis_manager_rejects_when_waiting_queue_is_full(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path, max_queued_tasks=1)
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    manager.create_video_frames_task(request, owner_id="a" * 32)

    with pytest.raises(TaskConflictError, match="queue is full"):
        manager.create_video_frames_task(request, owner_id="a" * 32)


def test_queue_cap_holds_under_concurrent_submissions(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path, max_queued_tasks=20)
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))

    def submit(_index: int) -> bool:
        try:
            manager.create_video_frames_task(request, owner_id="a" * 32)
        except TaskConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=30) as executor:
        accepted = list(executor.map(submit, range(30)))

    assert sum(accepted) == 20
    assert manager.queue.count == 20


def test_redis_log_store_persists_events_for_other_processes() -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    writer = RedisLogStore(redis_connection, retention_seconds=7200)
    reader = RedisLogStore(redis_connection, retention_seconds=7200)

    writer.append(
        task_id="task-1",
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="queued",
        details={"position": 1},
    )

    events = reader.history("task-1")
    assert len(events) == 1
    assert events[0].task_id == "task-1"
    assert events[0].details == {"position": 1}


def test_worker_status_updates_are_visible_to_api_manager(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    api_manager = _manager(redis_connection, tmp_path)
    worker_manager = _manager(redis_connection, tmp_path)
    task = api_manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    worker_manager.mark_running(task.task_id)
    worker_manager.mark_finished(
        task.task_id,
        status=TaskStatus.SUCCEEDED,
        output_path="/api/tasks/task/download",
        result_path=str(tmp_path / "result.zip"),
    )

    completed = api_manager.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.output_path == "/api/tasks/task/download"
    assert completed.result_path == str(tmp_path / "result.zip")
    assert completed.kind is TaskKind.VIDEO_FRAMES
    assert api_manager.active_owner_ids() == set()
    assert redis_connection.ttl(f"dt:task:{task.task_id}") > 0


def test_active_owner_ids_include_only_queued_or_running_tasks(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    queued = manager.create_video_frames_task(request, owner_id="a" * 32)

    assert manager.active_owner_ids() == {"a" * 32}

    manager.mark_finished(queued.task_id, status=TaskStatus.FAILED, error="failed")
    assert manager.active_owner_ids() == set()


def test_rate_limit_is_atomic_per_bucket(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)

    assert manager.allow_request("client:upload", limit=2, window=60)
    assert manager.allow_request("client:upload", limit=2, window=60)
    assert not manager.allow_request("client:upload", limit=2, window=60)
    assert manager.allow_request("other:upload", limit=2, window=60)


def test_batch_resize_disambiguates_duplicate_input_names(tmp_path: Path) -> None:
    options = media_resize_options_list_from_request(
        MediaResizeTaskRequest(
            input_paths=["/first/photo.png", "/second/photo.png"],
            output_dir=str(tmp_path),
            size="100x100",
        )
    )

    assert options[0].output_path == tmp_path / "001_photo_resized.png"
    assert options[1].output_path == tmp_path / "002_photo_resized.png"


def test_worker_executor_runs_reserved_task_and_persists_success(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    task = state.create_video_frames_task(request, owner_id="a" * 32)
    output_dir = tmp_path / "frames"
    executor = RedisTaskExecutor(state)

    def export(_options, _backend) -> ExportResult:
        output_dir.mkdir()
        (output_dir / "clip_000001.png").write_bytes(b"png")
        return ExportResult(
            output_dir=output_dir,
            output_pattern=output_dir / "clip_%06d.png",
            command=("ffmpeg",),
        )

    executor._exporter = export

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.output_path == f"/api/tasks/{task.task_id}/download"
    assert completed.result_path is not None
    assert Path(completed.result_path).is_file()


def test_worker_rejects_zip_larger_than_budget_after_packaging(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "assets" / "clip.mp4"
    input_path.parent.mkdir()
    input_path.write_bytes(b"input")
    output_dir = tmp_path / "frames"
    request = VideoFramesTaskRequest(
        input_path=str(input_path),
        output_dir=str(output_dir),
    )
    task = state.create_video_frames_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state, max_output_bytes=129)

    def export(_options, _backend) -> ExportResult:
        output_dir.mkdir()
        (output_dir / "frame.bin").write_bytes(bytes(range(128)))
        return ExportResult(
            output_dir=output_dir,
            output_pattern=output_dir / "clip_%06d.png",
            command=("ffmpeg",),
        )

    executor._exporter = export

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert completed.error is not None
    assert "129-byte limit" in completed.error
    assert not output_dir.exists()
    assert not (tmp_path / f"{task.task_id}.zip").exists()
    assert input_path.read_bytes() == b"input"


@pytest.mark.parametrize("failure_mode", ["nonzero", "exception"])
def test_worker_removes_partial_ffmpeg_output_after_any_failure(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "assets" / "clip.mp4"
    input_path.parent.mkdir()
    input_path.write_bytes(b"input")
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    request = MediaResizeTaskRequest(
        input_paths=[str(input_path)],
        output_dir=str(output_root),
        size="100x100",
    )
    task = state.create_media_resize_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state)

    class PartialFailureBackend:
        def get_ffmpeg_exe(self) -> str:
            return "ffmpeg"

        def run(self, command) -> ResizeBackendResult:
            Path(command[-1]).write_bytes(b"partial")
            if failure_mode == "exception":
                raise RuntimeError("FFmpeg process failed")
            return ResizeBackendResult(returncode=1, stderr="encode failed")

    executor._media_resizer = resize_media
    executor._backend_factory = PartialFailureBackend

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert not output_root.exists()
    assert input_path.read_bytes() == b"input"


def test_worker_failure_cleanup_preserves_input_inside_output_root(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    input_path = output_root / "clip.mp4"
    input_path.write_bytes(b"input")
    request = MediaResizeTaskRequest(
        input_paths=[str(input_path)],
        output_dir=str(output_root),
        size="100x100",
    )
    task = state.create_media_resize_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state)

    class PartialFailureBackend:
        def get_ffmpeg_exe(self) -> str:
            return "ffmpeg"

        def run(self, command) -> ResizeBackendResult:
            Path(command[-1]).write_bytes(b"partial")
            return ResizeBackendResult(returncode=1, stderr="encode failed")

    executor._media_resizer = resize_media
    executor._backend_factory = PartialFailureBackend

    executor.execute_reserved_task(task, request)

    assert input_path.read_bytes() == b"input"
    assert sorted(path.name for path in output_root.iterdir()) == ["clip.mp4"]


def test_worker_aborts_batch_when_aggregate_output_exceeds_budget(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    inputs = [tmp_path / "assets" / name for name in ("one.png", "two.png")]
    for input_path in inputs:
        input_path.parent.mkdir(exist_ok=True)
        input_path.write_bytes(b"input")
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    request = MediaResizeTaskRequest(
        input_paths=[str(path) for path in inputs],
        output_dir=str(output_root),
        size="100x100",
    )
    task = state.create_media_resize_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state, max_output_bytes=8)
    backend_limits: list[int | None] = []
    backends: list[object] = []

    def resize(options, backend) -> MediaResizeResult:
        assert backend._output_guard is not None
        backends.append(backend)
        backend_limits.append(backend._max_file_bytes)
        assert options.output_path is not None
        options.output_path.write_bytes(b"1234")
        return MediaResizeResult(output_path=options.output_path, command=("ffmpeg",))

    executor._media_resizer = resize

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert completed.error is not None
    assert "8-byte limit" in completed.error
    assert not output_root.exists()
    assert all(path.read_bytes() == b"input" for path in inputs)
    assert backend_limits == [7, 3]
    assert backends[0] is not backends[1]
    assert backends[0]._output_guard is backends[1]._output_guard


def test_worker_aborts_chroma_video_from_progress_accounting(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "assets" / "clip.mp4"
    input_path.parent.mkdir()
    input_path.write_bytes(b"input")
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    request = ChromaVideoKeyerTaskRequest(
        input_path=str(input_path),
        samples=[[0, 255, 0]],
        params={},
        output_root=str(output_root),
    )
    task = state.create_chroma_video_keyer_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state, max_output_bytes=6)

    def key_video(options, decoder, model, params, progress_callback):
        del options, decoder, model, params
        current_path = output_root / ".clip.tmp" / "clip_keyed_000001.png"
        current_path.parent.mkdir()
        current_path.write_bytes(b"123456")
        assert progress_callback is not None
        try:
            progress_callback(1, 2, current_path)
        except Exception as exc:
            raise KeyerError("Video PNG sequence export failed") from exc
        raise AssertionError("progress callback should abort processing")

    executor._video_keyer = key_video

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert completed.error is not None
    assert "6-byte limit" in completed.error
    assert not output_root.exists()
    assert input_path.read_bytes() == b"input"


def test_worker_final_tree_check_catches_unaccounted_output(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "assets" / "photo.png"
    input_path.parent.mkdir()
    input_path.write_bytes(b"input")
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    output_path = output_root / "photo_keyed.png"
    request = ChromaKeyerTaskRequest(
        input_path=str(input_path),
        samples=[[0, 255, 0]],
        params={},
        output_path=str(output_path),
    )
    task = state.create_chroma_keyer_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state, max_output_bytes=7)

    def key_image(options, model, params) -> KeyImageResult:
        del model, params
        assert options.output_path is not None
        options.output_path.write_bytes(b"12")
        (options.output_path.parent / "sidecar.bin").write_bytes(b"12345")
        return KeyImageResult(output_path=options.output_path)

    executor._keyer = key_image

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert completed.error is not None
    assert "7-byte limit" in completed.error
    assert not output_root.exists()
    assert input_path.read_bytes() == b"input"
