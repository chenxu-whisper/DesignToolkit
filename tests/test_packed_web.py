from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from designtoolkit.web.adapters import chroma_video_keyer_options_from_request
from designtoolkit.web.logs import LogStore
from designtoolkit.web.schemas import ChromaVideoKeyerTaskRequest, TaskKind, TaskStatus
from designtoolkit.web.tasks import TaskManager


def video_payload(input_path: str = "source.mp4") -> dict:
    return {
        "input_path": input_path,
        "samples": [[0, 255, 0]],
        "params": {},
    }


def test_legacy_video_request_defaults_to_png_sequence() -> None:
    request = ChromaVideoKeyerTaskRequest(**video_payload())
    assert request.output_format == "png_sequence"
    assert request.output_path is None


def test_packed_video_request_preserves_format_and_save_path(tmp_path: Path) -> None:
    output = tmp_path / "result.mp4"
    request = ChromaVideoKeyerTaskRequest(
        **video_payload(),
        output_format="packed_alpha_mp4",
        output_path=str(output),
    )
    options, _, _ = chroma_video_keyer_options_from_request(request)
    assert options.output_format == "packed_alpha_mp4"
    assert options.output_path == output


def test_unknown_video_output_format_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ChromaVideoKeyerTaskRequest(**video_payload(), output_format="native_alpha")


def test_png_sequence_rejects_video_save_path() -> None:
    with pytest.raises(ValidationError, match="output_path"):
        ChromaVideoKeyerTaskRequest(
            **video_payload(), output_format="png_sequence", output_path="result.mp4"
        )


def test_packed_save_path_cannot_replace_input() -> None:
    with pytest.raises(ValidationError, match="input"):
        ChromaVideoKeyerTaskRequest(
            **video_payload(),
            output_format="packed_alpha_mp4",
            output_path="source.mp4",
        )


def test_packed_task_uses_mp4_result_and_player_bundle(tmp_path: Path) -> None:
    calls = []
    output = tmp_path / "result.mp4"
    metadata = output.with_suffix(".json")

    def export(
        options, decoder, model, params, progress, *, output_check, stage_callback=None
    ):
        calls.append(options)
        output.write_bytes(b"mp4")
        metadata.write_text("{}", encoding="utf-8")
        output_check()
        progress(1, 1, output)
        if stage_callback:
            for stage in ("encoding", "audio", "publishing"):
                stage_callback(stage)
        return SimpleNamespace(
            output_path=output,
            metadata_path=metadata,
            output_dir=tmp_path,
            frame_count=1,
            metadata=SimpleNamespace(estimated_frame_count=1),
        )

    manager = TaskManager(
        log_store=LogStore(),
        packed_video_keyer=export,
        video_decoder_factory=lambda: object(),
        run_async=False,
    )
    task = manager.create_chroma_video_keyer_task(
        ChromaVideoKeyerTaskRequest(
            **video_payload(str(tmp_path / "input.mp4")),
            output_format="packed_alpha_mp4",
            output_path=str(output),
        )
    )
    assert task.status.value == "succeeded", task.error
    assert calls[0].output_format == "packed_alpha_mp4"
    assert str(output) in task.output_path
    assert str(metadata) in task.output_path
    assert (tmp_path / "result_player" / "index.html").is_file()
    assert not any(
        "_keyed_000001.png" in str(event.details)
        for event in manager.log_store.history(task.task_id)
    )
    assert [
        event.details["stage"]
        for event in manager.log_store.history(task.task_id)
        if "stage" in event.details
    ] == ["encoding", "audio", "publishing"]


def test_worker_accounts_explicit_mp4_output_parent(tmp_path: Path) -> None:
    from designtoolkit.web.worker import _request_output_root

    request = ChromaVideoKeyerTaskRequest(
        **video_payload(),
        output_format="packed_alpha_mp4",
        output_path=str(tmp_path / "result.mp4"),
    )
    assert _request_output_root(request) == tmp_path


def test_redis_reservation_checks_mp4_collision(tmp_path: Path) -> None:
    from designtoolkit.chroma_keyer.models import KeyerError
    from designtoolkit.web.redis_tasks import RedisTaskManager

    output = tmp_path / "result.mp4"
    output.write_bytes(b"existing")
    manager = SimpleNamespace(_enqueue=lambda *args, **kwargs: None)
    request = ChromaVideoKeyerTaskRequest(
        **video_payload(str(tmp_path / "input.mp4")),
        output_format="packed_alpha_mp4",
        output_path=str(output),
    )
    with pytest.raises(KeyerError, match="exists"):
        RedisTaskManager.create_chroma_video_keyer_task(manager, request)
    assert output.read_bytes() == b"existing"


def test_queued_video_request_roundtrip_preserves_format(tmp_path: Path) -> None:
    from designtoolkit.web.worker import _task_request

    request = ChromaVideoKeyerTaskRequest(
        **video_payload(),
        output_format="packed_alpha_mp4",
        output_path=str(tmp_path / "result.mp4"),
    )
    restored = _task_request("chroma_video_keyer", request.model_dump(mode="json"))
    assert restored == request


def test_mp4_save_picker_schema_and_default_suffix() -> None:
    from designtoolkit.web.native_picker import (
        _normalize_save_paths,
        _save_file_options,
        _save_filetypes,
    )
    from designtoolkit.web.schemas import LocalSaveFilePickRequest

    assert LocalSaveFilePickRequest(file_kind="video").file_kind == "video"
    assert _save_file_options("video") == {"defaultextension": ".mp4"}
    assert _save_filetypes("video") == [("MP4 files", "*.mp4")]
    assert _normalize_save_paths([Path("result")], "video") == [Path("result.mp4")]


def test_hosted_request_ignores_caller_output_paths(tmp_path: Path) -> None:
    from designtoolkit.web.packed_video import hosted_video_request

    request = ChromaVideoKeyerTaskRequest(
        **video_payload(),
        output_format="packed_alpha_mp4",
        output_path=str(tmp_path / "outside.mp4"),
        output_root=str(tmp_path / "outside"),
    )
    task_dir = tmp_path / "owned-task"
    asset = tmp_path / "asset.mp4"
    safe = hosted_video_request(request, asset, task_dir)
    assert Path(safe.output_path).parent == task_dir
    assert Path(safe.output_root) == task_dir
    assert safe.input_path == str(asset)
    assert safe.output_format == "packed_alpha_mp4"


@pytest.mark.parametrize("hosted", [False, True], ids=["local", "hosted"])
def test_player_publication_supports_valid_long_input_names(
    tmp_path: Path, hosted: bool
) -> None:
    from designtoolkit.web.packed_video import (
        hosted_video_request,
        packed_output_path,
        player_output_path,
        publish_player,
    )

    source = tmp_path / ("a" * 240 + ".mp4")
    source.write_bytes(b"source")
    request = ChromaVideoKeyerTaskRequest(
        **video_payload(str(source)), output_format="packed_alpha_mp4"
    )
    if hosted:
        request = hosted_video_request(request, source, tmp_path)
    options, _, _ = chroma_video_keyer_options_from_request(request)
    output = packed_output_path(options)
    destination = player_output_path(output)
    assert len(destination.name.encode("utf-8")) <= 255

    player = publish_player(output, lambda: None)

    assert player == destination
    assert (player / "index.html").is_file()
    assert source.read_bytes() == b"source"
    assert not list(tmp_path.glob(".*.tmp"))


def test_packed_bundle_failure_cleans_up_new_files(tmp_path: Path) -> None:
    output = tmp_path / "result.mp4"

    def fail(*args, **kwargs):
        raise RuntimeError("encoder failed")

    manager = TaskManager(
        log_store=LogStore(),
        packed_video_keyer=fail,
        video_decoder_factory=lambda: object(),
        run_async=False,
    )
    task = manager.create_chroma_video_keyer_task(
        ChromaVideoKeyerTaskRequest(
            **video_payload(str(tmp_path / "input.mp4")),
            output_format="packed_alpha_mp4",
            output_path=str(output),
        )
    )
    assert task.status.value == "failed"
    assert not (tmp_path / "result_player").exists()
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "failure_stage, blocked",
    [
        pytest.param("export", ("player",), id="export-player-locked"),
        pytest.param("output_check", ("player",), id="check-player-locked"),
        pytest.param("output_check", ("mp4",), id="check-mp4-locked"),
        pytest.param("output_check", ("metadata",), id="check-metadata-locked"),
        pytest.param(
            "output_check",
            ("player", "mp4", "metadata"),
            id="check-all-locked",
        ),
    ],
)
def test_async_packed_cleanup_failure_finalizes_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    blocked: tuple[str, ...],
) -> None:
    from threading import Thread

    import designtoolkit.web.tasks as tasks

    output = tmp_path / "result.mp4"
    metadata = output.with_suffix(".json")
    player = tmp_path / "result_player"
    owned = {"player": player, "mp4": output, "metadata": metadata}
    sibling = tmp_path / "existing.mp4"
    sibling.write_bytes(b"preserve")
    sibling_metadata = sibling.with_suffix(".json")
    sibling_metadata.write_bytes(b"preserve metadata")
    sibling_player = tmp_path / "existing_player"
    sibling_player.mkdir()
    (sibling_player / "index.html").write_bytes(b"preserve player")
    primary = RuntimeError(
        "encoder failed" if failure_stage == "export" else "quota exceeded"
    )
    export_completed = False
    failures = []
    threads = []
    thread_errors = []
    cleanup_attempts = []
    real_rmtree = tasks.rmtree
    real_unlink = Path.unlink

    class ObservedThread(Thread):
        def run(self):
            try:
                super().run()
            except Exception as exc:
                thread_errors.append(exc)

    def start_thread(**kwargs):
        thread = ObservedThread(**kwargs)
        threads.append(thread)
        return thread

    def remove_player(path):
        cleanup_attempts.append(path)
        if "player" in blocked:
            raise PermissionError("player locked")
        real_rmtree(path)

    def unlink(path, *args, **kwargs):
        if path in (output, metadata):
            cleanup_attempts.append(path)
            if path in [owned[name] for name in blocked]:
                raise PermissionError(f"{path.name} locked")
        return real_unlink(path, *args, **kwargs)

    class CheckedManager(TaskManager):
        def _check_output_tree(self):
            if export_completed:
                raise primary

        def _handle_packed_task_failure(self, task_id, exc):
            failures.append(exc)
            super()._handle_packed_task_failure(task_id, exc)

    def export(*args, **kwargs):
        nonlocal export_completed
        if failure_stage == "export":
            raise primary
        output.write_bytes(b"mp4")
        metadata.write_bytes(b"{}")
        export_completed = True
        return SimpleNamespace(output_path=output, metadata_path=metadata)

    monkeypatch.setattr(tasks, "Thread", start_thread)
    monkeypatch.setattr(tasks, "rmtree", remove_player)
    monkeypatch.setattr(Path, "unlink", unlink)
    manager = CheckedManager(
        log_store=LogStore(),
        packed_video_keyer=export,
        video_decoder_factory=lambda: object(),
    )
    task = manager.create_chroma_video_keyer_task(
        ChromaVideoKeyerTaskRequest(
            **video_payload(str(tmp_path / "input.mp4")),
            output_format="packed_alpha_mp4",
            output_path=str(output),
        )
    )
    assert len(threads) == 1
    threads[0].join(timeout=5)
    assert not threads[0].is_alive()
    assert task.status is TaskStatus.FAILED
    assert task.error == str(primary)
    assert task.finished_at is not None
    assert failures == [primary]
    assert len(primary.__notes__) == len(blocked)
    assert all("locked" in note for note in primary.__notes__)
    assert thread_errors == []
    assert cleanup_attempts == (
        [player] if failure_stage == "export" else [player, output, metadata]
    )
    for name, path in owned.items():
        assert path.exists() == (name in blocked)
    assert sibling.read_bytes() == b"preserve"
    assert sibling_metadata.read_bytes() == b"preserve metadata"
    assert (sibling_player / "index.html").read_bytes() == b"preserve player"
    assert manager._active_task_id is None
    next_task = manager.reserve_task(
        kind=TaskKind.CHROMA_KEYER, input_path="next.mp4", output_path=None
    )
    assert next_task.status is TaskStatus.QUEUED


def test_packed_export_through_local_http_api(tmp_path: Path) -> None:
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from designtoolkit.web.app import create_app

    output = tmp_path / "api.mp4"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    def export(
        options, decoder, model, params, progress, *, output_check, stage_callback=None
    ):
        output.write_bytes(b"encoded")
        metadata = output.with_suffix(".json")
        metadata.write_text("{}", encoding="utf-8")
        progress(2, 2, output)
        return SimpleNamespace(
            output_path=output,
            metadata_path=metadata,
            output_dir=tmp_path,
            frame_count=2,
            metadata=SimpleNamespace(estimated_frame_count=2),
        )

    manager = TaskManager(
        log_store=LogStore(), packed_video_keyer=export, run_async=False
    )
    async def exercise_api():
        transport = ASGITransport(app=create_app(manager, local_path_roots=[tmp_path]))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/tasks/chroma-keyer-video",
                json={
                    **video_payload(str(source)),
                    "output_format": "packed_alpha_mp4",
                    "output_path": str(output),
                },
            )
            assert response.status_code == 202, response.text
            status = await client.get(f"/api/tasks/{response.json()['task_id']}")
            assert status.status_code == 200
            assert status.json()["status"] == "succeeded"
            assert str(output) in status.json()["output_path"]

    asyncio.run(exercise_api())


def test_download_bundle_contains_one_mp4_metadata_and_player(tmp_path: Path) -> None:
    from zipfile import ZipFile

    from designtoolkit.web.hosted import prepare_download_artifact
    from designtoolkit.web.packed_video import publish_player

    output = tmp_path / "result.mp4"
    metadata = output.with_suffix(".json")
    output.write_bytes(b"mp4")
    metadata.write_text("{}", encoding="utf-8")
    player = publish_player(output, lambda: None)
    archive = prepare_download_artifact("packed", [output, metadata, player])
    with ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert [name for name in names if name.endswith(".mp4")] == ["result.mp4"]
        assert "result.json" in names
        assert "result_player/index.html" in names
        assert "result_player/player.js" in names


def test_worker_collision_preserves_existing_files(tmp_path: Path) -> None:
    from designtoolkit.web.worker import RedisTaskExecutor

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output_root = tmp_path / "output"
    output_root.mkdir()
    output = output_root / "result.mp4"
    output.write_bytes(b"existing output")
    other = output_root / "other.txt"
    other.write_text("owned by someone else")
    manager = TaskManager(log_store=LogStore(), run_async=False)
    task = manager.reserve_task(
        kind=TaskKind.CHROMA_KEYER,
        input_path=str(source),
        output_path=str(output),
    )

    def fail(task_id, message):
        task.status = TaskStatus.FAILED
        task.error = message

    state = SimpleNamespace(
        log_store=LogStore(),
        _finalize_external_failure=fail,
    )
    executor = RedisTaskExecutor(state, task_already_running=True)
    executor.execute_reserved_task(
        task,
        ChromaVideoKeyerTaskRequest(
            **video_payload(str(source)),
            output_format="packed_alpha_mp4",
            output_path=str(output),
        ),
    )
    assert task.status.value == "failed"
    assert output.read_bytes() == b"existing output"
    assert other.read_text() == "owned by someone else"


def test_external_worker_failure_cleanup_preserves_packed_collision(
    tmp_path: Path,
) -> None:
    from designtoolkit.web.worker import discard_failed_task_output

    output = tmp_path / "existing.mp4"
    output.write_bytes(b"existing")
    request = ChromaVideoKeyerTaskRequest(
        **video_payload(),
        output_format="packed_alpha_mp4",
        output_path=str(output),
    )
    state = SimpleNamespace(
        log_store=LogStore(),
        task_work=lambda task_id: (
            "chroma_video_keyer", request.model_dump(mode="json")
        ),
    )
    discard_failed_task_output("failed-task", state)
    assert output.read_bytes() == b"existing"


def test_worker_packaging_failure_only_removes_created_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import designtoolkit.web.worker as worker

    output_root = tmp_path / "output"
    output_root.mkdir()
    output = output_root / "created.mp4"
    other = output_root / "existing.txt"
    other.write_text("preserve")
    task = TaskManager(log_store=LogStore()).reserve_task(
        kind=TaskKind.CHROMA_KEYER, input_path="source.mp4", output_path=str(output)
    )

    def fail(task_id, message):
        task.status = TaskStatus.FAILED
        task.error = message

    def packaging_error(*args, **kwargs):
        raise RuntimeError("cannot package")

    class Executor(worker.RedisTaskExecutor):
        def _run_packed_video_keyer(self, task, options, model, params):
            output.write_bytes(b"created")
            self._mark_succeeded(task.task_id, [output])

    monkeypatch.setattr(worker, "prepare_download_artifact", packaging_error)
    executor = Executor(
        SimpleNamespace(log_store=LogStore(), _finalize_external_failure=fail),
        task_already_running=True,
    )
    executor.execute_reserved_task(
        task,
        ChromaVideoKeyerTaskRequest(
            **video_payload(),
            output_format="packed_alpha_mp4",
            output_path=str(output),
        ),
    )
    assert task.status is TaskStatus.FAILED
    assert not output.exists()
    assert other.read_text() == "preserve"
