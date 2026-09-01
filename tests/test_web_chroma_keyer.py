import asyncio
import json
from io import BytesIO
from math import inf, nan
from os import utime
from pathlib import Path
from queue import Empty
from threading import Event, Thread
from uuid import UUID

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image
from pytest import mark, raises

from designtoolkit.chroma_keyer.despill import despill
from designtoolkit.chroma_keyer.image_io import make_preview_png, read_rgb_image
from designtoolkit.chroma_keyer.matte import generate_alpha, refine_alpha
from designtoolkit.chroma_keyer.models import KeyerParams, KeyImageResult
from designtoolkit.chroma_keyer.sampling import screen_model_from_samples
from designtoolkit.web import routes as web_routes
from designtoolkit.web.adapters import chroma_keyer_options_from_request
from designtoolkit.web.app import create_app
from designtoolkit.web.chroma_previews import ChromaPreviewStore
from designtoolkit.web.logs import LogStore
from designtoolkit.web.schemas import (
    ChromaKeyerTaskRequest,
    ChromaPreviewRequest,
    LogLevel,
    LogPhase,
    TaskKind,
    TaskStatus,
)
from designtoolkit.web.tasks import TaskManager


def _write_input_image(path: Path) -> np.ndarray:
    pixels = np.array(
        [
            [[42, 180, 72], [220, 40, 40], [30, 50, 200], [10, 20, 30]],
            [[20, 30, 40], [50, 60, 70], [80, 90, 100], [110, 120, 130]],
        ],
        dtype=np.uint8,
    )
    Image.fromarray(pixels, mode="RGB").save(path)
    return pixels


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(local_path_roots=[tmp_path]))


def _decode_rgb_png(png_bytes: bytes) -> np.ndarray:
    with Image.open(BytesIO(png_bytes)) as image:
        return np.asarray(image.convert("RGB")).copy()


def _decode_rgba_png(png_bytes: bytes) -> np.ndarray:
    with Image.open(BytesIO(png_bytes)) as image:
        assert image.mode == "RGBA"
        return np.asarray(image).copy()


def _generated_alpha_preview(rgb: np.ndarray) -> bytes:
    model = screen_model_from_samples([[42, 180, 72]])
    alpha = refine_alpha(generate_alpha(rgb, model, KeyerParams()), KeyerParams())
    return make_preview_png(np.repeat(alpha[..., np.newaxis], 3, axis=2))


def _replace_image_preserving_metadata(path: Path, rgb: np.ndarray) -> None:
    source_stat = path.stat()
    buffer = BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    image_bytes = buffer.getvalue()
    assert len(image_bytes) <= source_stat.st_size
    path.write_bytes(image_bytes.ljust(source_stat.st_size, b"\0"))
    utime(
        path,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
    )


def test_chroma_keyer_task_request_accepts_the_default_export_contract() -> None:
    request = ChromaKeyerTaskRequest(
        input_path="input.png",
        samples=[[42, 180, 72]],
        params={},
    )

    assert request.model_dump() == {
        "input_path": "input.png",
        "samples": [[42, 180, 72]],
        "params": {
            "screen_gain": 1.0,
            "screen_balance": 0.5,
            "clip_black": 0.0,
            "clip_white": 1.0,
            "shrink_grow": 0,
            "feather": 0,
            "despill_amount": 1.0,
        },
        "output_path": None,
    }


@mark.parametrize(
    "changes",
    [
        {"samples": []},
        {"samples": [[42, 180, 72], [40, 182, 74]]},
        {"samples": [[42, 180]]},
        {"samples": [[-1, 180, 72]]},
        {"samples": [[42, 180, 256]]},
        {"samples": [[True, 180, 72]]},
        {"params": {"clip_black": 0.5, "clip_white": 0.5}},
        {"params": {"screen_gain": 2.1}},
        {"params": {"screen_gain": inf}},
        {"params": {"screen_gain": nan}},
        {"params": {"screen_gain": True}},
        {"params": {"screen_balance": 1.1}},
        {"params": {"screen_balance": inf}},
        {"params": {"screen_balance": nan}},
        {"params": {"screen_balance": True}},
        {"output_path": "   "},
    ],
)
def test_chroma_keyer_task_request_rejects_invalid_export_contract(
    changes: dict[str, object],
) -> None:
    with raises(ValueError):
        ChromaKeyerTaskRequest.model_validate(
            {
                "input_path": "input.png",
                "samples": [[42, 180, 72]],
                "params": {},
                **changes,
            }
        )


def test_chroma_keyer_task_request_requires_nested_params() -> None:
    with raises(ValueError):
        ChromaKeyerTaskRequest.model_validate(
            {
                "input_path": "input.png",
                "samples": [[42, 180, 72]],
            }
        )


def test_chroma_preview_request_requires_nested_params() -> None:
    with raises(ValueError):
        ChromaPreviewRequest.model_validate(
            {
                "input_path": "input.png",
                "samples": [[42, 180, 72]],
            }
        )


@mark.parametrize("samples", [[], [[42, 180, 72], [40, 182, 74]]])
def test_chroma_preview_request_requires_exactly_one_sample(
    samples: list[list[int]],
) -> None:
    with raises(ValueError):
        ChromaPreviewRequest.model_validate(
            {
                "input_path": "input.png",
                "samples": samples,
                "params": {},
            }
        )


@mark.parametrize("legacy_field", ["tolerance", "softness"])
@mark.parametrize(
    "request_model",
    [ChromaKeyerTaskRequest, ChromaPreviewRequest],
)
def test_chroma_requests_reject_removed_nested_params(
    legacy_field: str,
    request_model: type[ChromaKeyerTaskRequest] | type[ChromaPreviewRequest],
) -> None:
    with raises(ValueError):
        request_model.model_validate(
            {
                "input_path": "input.png",
                "samples": [[42, 180, 72]],
                "params": {legacy_field: 0.4},
            }
        )


@mark.parametrize(
    ("legacy_field", "legacy_value"),
    [
        ("screen_gain", 1.4),
        ("screen_balance", 0.2),
        ("clip_black", 0.1),
        ("clip_white", 0.9),
        ("shrink_grow", -1),
        ("feather", 1),
        ("despill_amount", 0.75),
    ],
)
def test_chroma_keyer_task_request_rejects_legacy_flattened_params(
    legacy_field: str,
    legacy_value: float | int,
) -> None:
    with raises(ValueError):
        ChromaKeyerTaskRequest.model_validate(
            {
                "input_path": "input.png",
                "samples": [[42, 180, 72]],
                "params": {},
                legacy_field: legacy_value,
            }
        )


@mark.parametrize(
    ("legacy_field", "legacy_value"),
    [
        ("screen_gain", 1.4),
        ("screen_balance", 0.2),
        ("clip_black", 0.1),
        ("clip_white", 0.9),
        ("shrink_grow", -1),
        ("feather", 1),
        ("despill_amount", 0.75),
    ],
)
def test_chroma_preview_request_rejects_legacy_flattened_params(
    legacy_field: str,
    legacy_value: float | int,
) -> None:
    with raises(ValueError):
        ChromaPreviewRequest.model_validate(
            {
                "input_path": "input.png",
                "samples": [[42, 180, 72]],
                "params": {},
                legacy_field: legacy_value,
            }
        )


def test_chroma_keyer_adapter_converts_request_to_core_keying_arguments() -> None:
    arguments = chroma_keyer_options_from_request(
        ChromaKeyerTaskRequest(
            input_path="screen.png",
            output_path="keyed.png",
            samples=[[42, 180, 72]],
            params={
                "screen_gain": 1.4,
                "screen_balance": 0.2,
                "clip_black": 0.1,
                "clip_white": 0.9,
                "shrink_grow": -2,
                "feather": 3,
                "despill_amount": 0.75,
            },
        )
    )
    options, model, params = arguments

    assert options.input_path == Path("screen.png")
    assert options.output_path == Path("keyed.png")
    assert model.samples == ((42, 180, 72),)
    assert params == KeyerParams(
        screen_gain=1.4,
        screen_balance=0.2,
        clip_black=0.1,
        clip_white=0.9,
        shrink_grow=-2,
        feather=3,
        despill_amount=0.75,
    )


def test_chroma_keyer_task_manager_exports_default_source_sized_rgba_png(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    _write_input_image(input_path)
    log_store = LogStore()
    task_manager = TaskManager(log_store=log_store, run_async=False)

    task = task_manager.create_chroma_keyer_task(
        ChromaKeyerTaskRequest(
            input_path=str(input_path),
            samples=[[42, 180, 72]],
            params={},
        )
    )

    output_path = tmp_path / "screen_keyed.png"
    assert task.kind is TaskKind.CHROMA_KEYER
    assert task.status is TaskStatus.SUCCEEDED
    assert task.output_path == str(output_path)
    with Image.open(output_path) as image:
        assert image.mode == "RGBA"
        assert image.size == (4, 2)
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((1, 0))[3] > 0

    events = log_store.history(task.task_id)
    assert [(event.level, event.phase) for event in events] == [
        (LogLevel.INFO, LogPhase.CREATED),
        (LogLevel.PROGRESS, LogPhase.RUNNING),
        (LogLevel.SUCCESS, LogPhase.RUNNING),
        (LogLevel.SUCCESS, LogPhase.COMPLETED),
    ]
    assert events[-1].details["elapsed_seconds"] >= 0


def test_chroma_keyer_task_endpoint_creates_and_reports_completed_task(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    _write_input_image(input_path)
    task_manager = TaskManager(log_store=LogStore(), run_async=False)
    client = TestClient(
        create_app(task_manager=task_manager, local_path_roots=[tmp_path])
    )

    response = client.post(
        "/api/tasks/chroma-keyer",
        json={
            "input_path": str(input_path),
            "samples": [[42, 180, 72]],
            "params": {},
        },
    )

    assert response.status_code == 200
    created_task = response.json()
    assert created_task["status"] == "succeeded"
    status = client.get(f"/api/tasks/{created_task['task_id']}")
    assert status.status_code == 200
    assert status.json()["kind"] == "chroma_keyer"
    assert status.json()["status"] == "succeeded"


def test_chroma_task_event_stream_emits_running_success_before_completed(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    _write_input_image(input_path)
    task_manager = TaskManager(log_store=LogStore(), run_async=False)
    task = task_manager.create_chroma_keyer_task(
        ChromaKeyerTaskRequest(
            input_path=str(input_path),
            samples=[[42, 180, 72]],
            params={},
        )
    )
    client = TestClient(
        create_app(task_manager=task_manager, local_path_roots=[tmp_path])
    )

    response = client.get(f"/api/tasks/{task.task_id}/events")

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert [(event["level"], event["phase"]) for event in events] == [
        ("info", "created"),
        ("progress", "running"),
        ("success", "running"),
        ("success", "completed"),
    ]


@mark.parametrize(
    ("status", "terminal_phase"),
    [
        (TaskStatus.SUCCEEDED, LogPhase.COMPLETED),
        (TaskStatus.FAILED, LogPhase.FAILED),
    ],
)
def test_task_event_stream_waits_for_terminal_event_after_task_is_finished(
    monkeypatch,
    status: TaskStatus,
    terminal_phase: LogPhase,
) -> None:
    log_store = LogStore()
    task_manager = TaskManager(log_store=log_store)
    task = task_manager.reserve_task(
        kind=TaskKind.CHROMA_KEYER,
        input_path="input.png",
        output_path="output.png",
    )
    terminal_publication_blocked = Event()
    release_terminal_publication = Event()
    terminal_status_timeout_seen = Event()
    stream_closed = Event()
    stream_chunks: list[bytes] = []
    original_append = log_store.append
    original_history_and_subscribe = log_store.history_and_subscribe

    def pause_terminal_append(**kwargs):
        if kwargs["phase"] is terminal_phase:
            terminal_publication_blocked.set()
            assert release_terminal_publication.wait(timeout=1)
        return original_append(**kwargs)

    def subscribe_with_terminal_gap_timeout(task_id: str):
        history, subscriber = original_history_and_subscribe(task_id)
        original_get = subscriber.get
        timed_out = False

        def get(*, timeout: float):
            nonlocal timed_out
            if not timed_out:
                timed_out = True
                terminal_status_timeout_seen.set()
                raise Empty
            return original_get(timeout=timeout)

        monkeypatch.setattr(subscriber, "get", get)
        return history, subscriber

    monkeypatch.setattr(log_store, "append", pause_terminal_append)
    monkeypatch.setattr(
        log_store,
        "history_and_subscribe",
        subscribe_with_terminal_gap_timeout,
    )

    def finish_task() -> None:
        if status is TaskStatus.SUCCEEDED:
            task_manager._mark_succeeded(task.task_id, "output.png")
        else:
            task_manager._mark_failed(task.task_id, "keying failed")

    def consume_stream() -> None:
        app = create_app(task_manager=task_manager)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/api/tasks/{task.task_id}/events",
            "raw_path": f"/api/tasks/{task.task_id}/events".encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                stream_chunks.append(message["body"])
                if not message["more_body"]:
                    stream_closed.set()

        asyncio.run(app(scope, receive, send))

    finish_thread = Thread(target=finish_task)
    finish_thread.start()
    assert terminal_publication_blocked.wait(timeout=1)
    assert task_manager.get_task(task.task_id).status is status

    stream_thread = Thread(target=consume_stream)
    stream_thread.start()
    assert terminal_status_timeout_seen.wait(timeout=1)
    try:
        assert not stream_closed.wait(timeout=0.25)
    finally:
        release_terminal_publication.set()
        finish_thread.join(timeout=1)
        stream_thread.join(timeout=1)

    assert not finish_thread.is_alive()
    assert not stream_thread.is_alive()
    events = [
        json.loads(line.removeprefix("data: "))
        for line in b"".join(stream_chunks).decode().splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["phase"] == terminal_phase.value
    assert stream_closed.is_set()


def test_chroma_keyer_task_endpoint_rejects_existing_output_without_overwrite(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    _write_input_image(input_path)
    output_path = tmp_path / "screen_keyed.png"
    original_bytes = b"existing keyed image"
    output_path.write_bytes(original_bytes)
    client = TestClient(
        create_app(
            task_manager=TaskManager(log_store=LogStore(), run_async=False),
            local_path_roots=[tmp_path],
        )
    )

    response = client.post(
        "/api/tasks/chroma-keyer",
        json={
            "input_path": str(input_path),
            "samples": [[42, 180, 72]],
            "params": {},
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "output_exists",
            "message": "Output path already exists",
        }
    }
    assert output_path.read_bytes() == original_bytes


def test_chroma_keyer_task_endpoint_rejects_invalid_nested_and_flattened_params(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    _write_input_image(input_path)
    client = TestClient(
        create_app(
            task_manager=TaskManager(log_store=LogStore(), run_async=False),
            local_path_roots=[tmp_path],
        )
    )
    valid_request = {
        "input_path": str(input_path),
        "samples": [[42, 180, 72]],
        "params": {},
    }

    invalid_nested_response = client.post(
        "/api/tasks/chroma-keyer",
        json={
            **valid_request,
            "params": {"clip_black": 0.5, "clip_white": 0.5},
        },
    )
    flattened_response = client.post(
        "/api/tasks/chroma-keyer",
        json={**valid_request, "screen_gain": 1.4},
    )

    assert invalid_nested_response.status_code == 400
    assert invalid_nested_response.json()["error"]["code"] == "invalid_request"
    assert flattened_response.status_code == 400
    assert flattened_response.json()["error"]["code"] == "invalid_request"


def test_chroma_keyer_task_endpoint_returns_conflict_until_blocked_task_succeeds(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    _write_input_image(input_path)
    keyer_started = Event()
    release_keyer = Event()
    task_finished = Event()
    log_store = LogStore()

    def blocking_keyer(options, _model, _params) -> KeyImageResult:
        keyer_started.set()
        assert release_keyer.wait(timeout=1)
        return KeyImageResult(output_path=tmp_path / "screen_keyed.png")

    class SignallingTaskManager(TaskManager):
        def mark_finished(
            self,
            task_id: str,
            *,
            status: TaskStatus,
            output_path: str | None = None,
            error: str | None = None,
        ):
            task = super().mark_finished(
                task_id,
                status=status,
                output_path=output_path,
                error=error,
            )
            task_finished.set()
            return task

    task_manager = SignallingTaskManager(
        log_store=log_store,
        keyer=blocking_keyer,
        run_async=True,
    )
    client = TestClient(
        create_app(task_manager=task_manager, local_path_roots=[tmp_path])
    )
    request_body = {
        "input_path": str(input_path),
        "samples": [[42, 180, 72]],
        "params": {},
    }

    first_response = client.post("/api/tasks/chroma-keyer", json=request_body)

    assert first_response.status_code == 200
    first_task_id = first_response.json()["task_id"]
    assert keyer_started.wait(timeout=1)
    assert [
        (event.level, event.phase)
        for event in log_store.history(first_task_id)
    ] == [
        (LogLevel.INFO, LogPhase.CREATED),
        (LogLevel.PROGRESS, LogPhase.RUNNING),
    ]

    second_response = client.post("/api/tasks/chroma-keyer", json=request_body)

    assert second_response.status_code == 409
    assert second_response.json() == {
        "error": {
            "code": "task_conflict",
            "message": "A task is already running",
        }
    }

    completion_events = log_store.subscribe(first_task_id)
    release_keyer.set()
    assert completion_events.get(timeout=1).phase is LogPhase.RUNNING
    assert completion_events.get(timeout=1).phase is LogPhase.COMPLETED
    assert task_finished.wait(timeout=1)
    assert task_manager.get_task(first_task_id).status is TaskStatus.SUCCEEDED


def test_chroma_keyer_terminal_event_observes_finished_task_and_free_slot(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    _write_input_image(input_path)
    keyer_started = Event()
    release_keyer = Event()
    log_store = LogStore()

    def blocking_keyer(options, _model, _params) -> KeyImageResult:
        keyer_started.set()
        assert release_keyer.wait(timeout=1)
        return KeyImageResult(output_path=tmp_path / "screen_keyed.png")

    task_manager = TaskManager(
        log_store=log_store,
        keyer=blocking_keyer,
        run_async=True,
    )
    first_task = task_manager.create_chroma_keyer_task(
        ChromaKeyerTaskRequest(
            input_path=str(input_path),
            samples=[[42, 180, 72]],
            params={},
        )
    )
    assert keyer_started.wait(timeout=1)

    class TerminalEventProbe:
        def __init__(self) -> None:
            self.delivered = Event()
            self.status: TaskStatus | None = None
            self.output_path: str | None = None
            self.finished_at = None
            self.error: str | None = None
            self.event = None
            self.reserved_task_id: str | None = None
            self.reservation_error: Exception | None = None

        def put(self, event) -> None:
            if event.phase is not LogPhase.COMPLETED:
                return
            task = task_manager.get_task(first_task.task_id)
            assert task is not None
            self.status = task.status
            self.output_path = task.output_path
            self.finished_at = task.finished_at
            self.error = task.error
            self.event = event
            try:
                reserved_task = task_manager.reserve_task(
                    kind=TaskKind.CHROMA_KEYER,
                    input_path=str(input_path),
                    output_path=str(tmp_path / "next_keyed.png"),
                )
            except Exception as exc:
                self.reservation_error = exc
            else:
                self.reserved_task_id = reserved_task.task_id
            self.delivered.set()

    probe = TerminalEventProbe()
    with log_store._lock:
        log_store._subscribers[first_task.task_id].append(probe)

    release_keyer.set()

    assert probe.delivered.wait(timeout=1)
    assert probe.status is TaskStatus.SUCCEEDED
    assert probe.output_path == str(tmp_path / "screen_keyed.png")
    assert probe.finished_at is not None
    assert probe.error is None
    assert probe.event.details["elapsed_seconds"] >= 0
    assert probe.reservation_error is None
    assert probe.reserved_task_id is not None


def test_inspect_returns_dimensions_and_a_retrievable_preview(tmp_path: Path) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    client = _client(tmp_path)

    response = client.post(
        "/api/chroma-keyer/images/inspect",
        json={"input_path": str(input_path)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "input_path": str(input_path),
        "width": 4,
        "height": 2,
        "preview_width": 4,
        "preview_height": 2,
        "preview_url": body["preview_url"],
    }
    assert body["preview_url"].startswith("/api/chroma-keyer/previews/")

    preview = client.get(body["preview_url"])

    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_inspect_succeeds_when_eviction_interleaves_after_cache_insertion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    original_get_or_put_entry = ChromaPreviewStore.get_or_put_entry
    calls = 0

    def evict_after_cache_insertion(store, cache_key, create_png_bytes):
        nonlocal calls
        calls += 1
        preview_entry = original_get_or_put_entry(store, cache_key, create_png_bytes)
        for index in range(32):
            store.put(f"eviction-{index}".encode())
        return preview_entry

    monkeypatch.setattr(
        ChromaPreviewStore,
        "get_or_put_entry",
        evict_after_cache_insertion,
    )
    client = TestClient(
        create_app(local_path_roots=[tmp_path]),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/chroma-keyer/images/inspect",
        json={"input_path": str(input_path)},
    )

    assert response.status_code == 200
    assert calls == 1


def test_inspect_preview_reuses_file_fingerprint_and_changes_after_source_update(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    pixels = _write_input_image(input_path)
    client = _client(tmp_path)
    payload = {"input_path": str(input_path)}

    first_response = client.post("/api/chroma-keyer/images/inspect", json=payload)
    duplicate_response = client.post(
        "/api/chroma-keyer/images/inspect",
        json=payload,
    )

    first_url = first_response.json()["preview_url"]
    assert first_response.status_code == 200
    assert duplicate_response.status_code == 200
    assert duplicate_response.json()["preview_url"] == first_url
    UUID(first_url.rsplit("/", maxsplit=1)[1])
    assert str(input_path) not in first_url

    source_stat = input_path.stat()
    pixels[0, 0] = [1, 2, 3]
    Image.fromarray(pixels, mode="RGB").save(input_path)
    utime(
        input_path,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1_000_000_000),
    )

    updated_response = client.post("/api/chroma-keyer/images/inspect", json=payload)

    assert updated_response.status_code == 200
    assert updated_response.json()["preview_url"] != first_url


@mark.parametrize(
    ("url", "payload", "make_expected_preview"),
    [
        ("/api/chroma-keyer/images/inspect", {}, make_preview_png),
        (
            "/api/chroma-keyer/previews",
            {"samples": [[42, 180, 72]], "params": {}, "view_mode": "alpha"},
            _generated_alpha_preview,
        ),
    ],
)
def test_preview_cache_distinguishes_content_with_identical_source_metadata(
    tmp_path: Path,
    url: str,
    payload: dict[str, object],
    make_expected_preview,
) -> None:
    input_path = tmp_path / "input.png"
    pixels = _write_input_image(input_path)
    input_path.write_bytes(input_path.read_bytes() + (b"\0" * 2_048))
    replacement_pixels = pixels.copy()
    replacement_pixels[0, 0] = [220, 40, 40]
    client = _client(tmp_path)

    first_response = client.post(url, json={"input_path": str(input_path), **payload})
    source_stat = input_path.stat()
    _replace_image_preserving_metadata(input_path, replacement_pixels)
    replacement_stat = input_path.stat()
    replacement_response = client.post(
        url,
        json={"input_path": str(input_path), **payload},
    )

    assert first_response.status_code == 200
    assert replacement_response.status_code == 200
    assert replacement_stat.st_size == source_stat.st_size
    assert replacement_stat.st_mtime_ns == source_stat.st_mtime_ns
    assert replacement_response.json()["preview_url"] != first_response.json()[
        "preview_url"
    ]
    preview = client.get(replacement_response.json()["preview_url"])
    assert np.array_equal(
        _decode_rgb_png(preview.content),
        _decode_rgb_png(make_expected_preview(replacement_pixels)),
    )


@mark.parametrize(
    ("url", "payload", "make_expected_preview"),
    [
        ("/api/chroma-keyer/images/inspect", {}, make_preview_png),
        (
            "/api/chroma-keyer/previews",
            {"samples": [[42, 180, 72]], "params": {}, "view_mode": "alpha"},
            _generated_alpha_preview,
        ),
    ],
)
def test_preview_snapshot_never_caches_aba_decoded_bytes_under_restored_source(
    tmp_path: Path,
    monkeypatch,
    url: str,
    payload: dict[str, object],
    make_expected_preview,
) -> None:
    input_path = tmp_path / "input.png"
    original_pixels = _write_input_image(input_path)
    input_path.write_bytes(input_path.read_bytes() + (b"\0" * 2_048))
    replacement_pixels = original_pixels.copy()
    replacement_pixels[0, 0] = [220, 40, 40]
    original_read_rgb_image = web_routes.read_rgb_image

    def read_replacement_then_restore(path: Path) -> np.ndarray:
        _replace_image_preserving_metadata(input_path, replacement_pixels)
        try:
            return original_read_rgb_image(path)
        finally:
            _replace_image_preserving_metadata(input_path, original_pixels)

    def decode_snapshot_during_aba(
        source_bytes: bytes,
        source_suffix: str,
    ) -> np.ndarray:
        assert source_suffix == ".png"
        _replace_image_preserving_metadata(input_path, replacement_pixels)
        _replace_image_preserving_metadata(input_path, original_pixels)
        with Image.open(BytesIO(source_bytes)) as image:
            return np.asarray(image.convert("RGB"), dtype=np.uint8)

    monkeypatch.setattr(web_routes, "read_rgb_image", read_replacement_then_restore)
    monkeypatch.setattr(
        web_routes,
        "read_rgb_image_bytes",
        decode_snapshot_during_aba,
        raising=False,
    )
    client = _client(tmp_path)
    request_body = {"input_path": str(input_path), **payload}

    first_response = client.post(url, json=request_body)
    second_response = client.post(url, json=request_body)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["preview_url"] == first_response.json()[
        "preview_url"
    ]
    preview = client.get(first_response.json()["preview_url"])
    assert np.array_equal(
        _decode_rgb_png(preview.content),
        _decode_rgb_png(make_expected_preview(original_pixels)),
    )


@mark.parametrize(
    ("url", "payload", "make_expected_preview"),
    [
        ("/api/chroma-keyer/images/inspect", {}, make_preview_png),
        (
            "/api/chroma-keyer/previews",
            {"samples": [[42, 180, 72]], "params": {}, "view_mode": "alpha"},
            _generated_alpha_preview,
        ),
    ],
)
def test_preview_retries_decode_when_source_changes_during_snapshot(
    tmp_path: Path,
    monkeypatch,
    url: str,
    payload: dict[str, object],
    make_expected_preview,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    replacement_pixels = np.array(
        [
            [[220, 40, 40], [220, 40, 40], [220, 40, 40]],
        ],
        dtype=np.uint8,
    )
    original_read_rgb_image_bytes = getattr(
        web_routes,
        "read_rgb_image_bytes",
        None,
    )
    decode_calls = 0

    def overwrite_after_first_decode(
        source_bytes: bytes,
        source_suffix: str,
    ) -> np.ndarray:
        nonlocal decode_calls
        assert original_read_rgb_image_bytes is not None
        rgb = original_read_rgb_image_bytes(source_bytes, source_suffix)
        decode_calls += 1
        if decode_calls == 1:
            original_stat = input_path.stat()
            Image.fromarray(replacement_pixels, mode="RGB").save(input_path)
            utime(
                input_path,
                ns=(
                    original_stat.st_atime_ns,
                    original_stat.st_mtime_ns + 1_000_000_000,
                ),
            )
        return rgb

    monkeypatch.setattr(
        web_routes,
        "read_rgb_image_bytes",
        overwrite_after_first_decode,
        raising=False,
    )
    client = _client(tmp_path)

    response = client.post(url, json={"input_path": str(input_path), **payload})

    assert response.status_code == 200
    assert decode_calls == 2
    preview = client.get(response.json()["preview_url"])
    assert np.array_equal(
        _decode_rgb_png(preview.content),
        _decode_rgb_png(make_expected_preview(replacement_pixels)),
    )


@mark.parametrize(
    ("url", "payload", "make_expected_preview"),
    [
        ("/api/chroma-keyer/images/inspect", {}, make_preview_png),
        (
            "/api/chroma-keyer/previews",
            {"samples": [[42, 180, 72]], "params": {}, "view_mode": "alpha"},
            _generated_alpha_preview,
        ),
    ],
)
def test_preview_retries_failed_decode_after_source_update(
    tmp_path: Path,
    monkeypatch,
    url: str,
    payload: dict[str, object],
    make_expected_preview,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    replacement_pixels = np.array(
        [
            [[220, 40, 40], [220, 40, 40], [220, 40, 40]],
        ],
        dtype=np.uint8,
    )
    original_read_rgb_image_bytes = getattr(
        web_routes,
        "read_rgb_image_bytes",
        None,
    )
    decode_calls = 0

    def corrupt_then_replace_during_first_decode(
        source_bytes: bytes,
        source_suffix: str,
    ) -> np.ndarray:
        nonlocal decode_calls
        decode_calls += 1
        if decode_calls == 1:
            source_stat = input_path.stat()
            input_path.write_bytes(b"not an image")
            try:
                assert original_read_rgb_image_bytes is not None
                return original_read_rgb_image_bytes(source_bytes, source_suffix)
            finally:
                Image.fromarray(replacement_pixels, mode="RGB").save(input_path)
                utime(
                    input_path,
                    ns=(
                        source_stat.st_atime_ns,
                        source_stat.st_mtime_ns + 1_000_000_000,
                    ),
                )
        assert original_read_rgb_image_bytes is not None
        return original_read_rgb_image_bytes(source_bytes, source_suffix)

    monkeypatch.setattr(
        web_routes,
        "read_rgb_image_bytes",
        corrupt_then_replace_during_first_decode,
        raising=False,
    )
    client = _client(tmp_path)

    response = client.post(url, json={"input_path": str(input_path), **payload})

    assert response.status_code == 200
    assert decode_calls == 2
    preview = client.get(response.json()["preview_url"])
    assert np.array_equal(
        _decode_rgb_png(preview.content),
        _decode_rgb_png(make_expected_preview(replacement_pixels)),
    )


def test_sample_maps_preview_coordinates_to_source_pixel_with_uppercase_hex(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)

    response = _client(tmp_path).post(
        "/api/chroma-keyer/images/sample",
        json={
            "input_path": str(input_path),
            "x": 0.1,
            "y": 0.1,
            "preview_width": 2,
            "preview_height": 1,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "rgb": [42, 180, 72],
        "hex": "#2AB448",
        "source_x": 0,
        "source_y": 0,
    }


def test_sample_rejects_low_saturation_without_returning_rgb(tmp_path: Path) -> None:
    input_path = tmp_path / "gray.png"
    Image.fromarray(
        np.array([[[128, 128, 128]]], dtype=np.uint8),
        mode="RGB",
    ).save(input_path)

    response = _client(tmp_path).post(
        "/api/chroma-keyer/images/sample",
        json={
            "input_path": str(input_path),
            "x": 0,
            "y": 0,
            "preview_width": 1,
            "preview_height": 1,
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "invalid_screen_color",
            "message": "请选择具有明显主色的纯色幕背景",
        }
    }


def test_sample_accepts_fractional_preview_dimensions_at_the_near_edge(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    pixels = _write_input_image(input_path)
    pixels[1, 3] = [110, 180, 120]
    Image.fromarray(pixels, mode="RGB").save(input_path)

    response = _client(tmp_path).post(
        "/api/chroma-keyer/images/sample",
        json={
            "input_path": str(input_path),
            "x": 400,
            "y": 200,
            "preview_width": 400.5,
            "preview_height": 200.5,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "rgb": [110, 180, 120],
        "hex": "#6EB478",
        "source_x": 3,
        "source_y": 1,
    }


def test_final_and_alpha_previews_match_the_public_keying_pipeline(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    source_rgb = np.array(
        [[[0, 255, 0], [180, 235, 180], [255, 0, 0]]],
        dtype=np.uint8,
    )
    Image.fromarray(source_rgb, mode="RGB").save(input_path)
    client = _client(tmp_path)
    payload = {
        "input_path": str(input_path),
        "samples": [[0, 255, 0]],
        "params": {
            "screen_gain": 1.0,
            "screen_balance": 0.5,
            "clip_black": 0.0,
            "clip_white": 1.0,
            "shrink_grow": 0,
            "feather": 0,
            "despill_amount": 1.0,
        },
    }

    final_response = client.post("/api/chroma-keyer/previews", json=payload)
    alpha_response = client.post(
        "/api/chroma-keyer/previews",
        json={**payload, "view_mode": "alpha"},
    )

    assert final_response.status_code == 200
    assert alpha_response.status_code == 200
    final_png = client.get(final_response.json()["preview_url"])
    alpha_png = client.get(alpha_response.json()["preview_url"])
    assert final_png.headers["content-type"] == "image/png"
    assert alpha_png.headers["content-type"] == "image/png"
    assert final_png.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert alpha_png.content.startswith(b"\x89PNG\r\n\x1a\n")

    final_rgba = _decode_rgba_png(final_png.content)
    with Image.open(BytesIO(alpha_png.content)) as alpha_image:
        assert alpha_image.mode == "RGB"
        assert "transparency" not in alpha_image.info
        alpha_preview = np.asarray(alpha_image).copy()

    source_rgb = read_rgb_image(input_path)
    model = screen_model_from_samples(payload["samples"])
    params = KeyerParams(**payload["params"])
    refined_alpha = refine_alpha(generate_alpha(source_rgb, model, params), params)
    expected_final = _decode_rgb_png(
        make_preview_png(
            despill(source_rgb, refined_alpha, model, params.despill_amount)
        )
    )
    expected_alpha = _decode_rgb_png(
        make_preview_png(np.repeat(refined_alpha[..., np.newaxis], 3, axis=2))
    )

    assert np.array_equal(final_rgba[..., :3], expected_final)
    assert np.array_equal(final_rgba[..., 3], refined_alpha)
    assert final_rgba[0, 0, 3] == 0
    assert 0 < final_rgba[0, 1, 3] < 255
    assert final_rgba[0, 1, 1] < source_rgb[0, 1, 1]
    assert np.array_equal(alpha_preview, expected_alpha)


def test_final_and_alpha_previews_use_shared_key_rgb_pipeline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    input_path = tmp_path / "input.png"
    source_rgb = _write_input_image(input_path)
    expected_keyed_rgb = np.array(
        [
            [[10, 20, 30], [40, 50, 60], [70, 80, 90], [100, 110, 120]],
            [
                [130, 140, 150],
                [160, 170, 180],
                [190, 200, 210],
                [220, 230, 240],
            ],
        ],
        dtype=np.uint8,
    )
    expected_alpha = np.array(
        [[0, 32, 96, 255], [255, 192, 128, 64]],
        dtype=np.uint8,
    )
    pipeline_calls: list[tuple[np.ndarray, KeyerParams]] = []

    def key_rgb(
        rgb: np.ndarray,
        _model,
        params: KeyerParams,
    ) -> tuple[np.ndarray, np.ndarray]:
        pipeline_calls.append((rgb.copy(), params))
        return expected_keyed_rgb, expected_alpha

    monkeypatch.setattr(web_routes, "key_rgb", key_rgb, raising=False)
    client = _client(tmp_path)
    payload = {
        "input_path": str(input_path),
        "samples": [[42, 180, 72]],
        "params": {},
    }

    final_response = client.post("/api/chroma-keyer/previews", json=payload)
    alpha_response = client.post(
        "/api/chroma-keyer/previews",
        json={**payload, "view_mode": "alpha"},
    )

    assert final_response.status_code == 200
    assert alpha_response.status_code == 200
    assert len(pipeline_calls) == 2
    assert all(np.array_equal(rgb, source_rgb) for rgb, _ in pipeline_calls)
    assert all(params == KeyerParams() for _, params in pipeline_calls)
    assert np.array_equal(
        _decode_rgba_png(client.get(final_response.json()["preview_url"]).content),
        np.dstack((expected_keyed_rgb, expected_alpha)),
    )
    assert np.array_equal(
        _decode_rgb_png(client.get(alpha_response.json()["preview_url"]).content),
        np.repeat(expected_alpha[..., np.newaxis], 3, axis=2),
    )


def test_generated_preview_reuses_request_state_and_varies_by_view_and_params(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    client = _client(tmp_path)
    payload = {
        "input_path": str(input_path),
        "samples": [[42, 180, 72]],
        "params": {
            "screen_gain": 1.25,
            "screen_balance": 0.35,
            "clip_black": 0.0,
            "clip_white": 1.0,
            "shrink_grow": 0,
            "feather": 0,
            "despill_amount": 0.5,
        },
    }

    first_response = client.post("/api/chroma-keyer/previews", json=payload)
    duplicate_response = client.post("/api/chroma-keyer/previews", json=payload)
    alpha_response = client.post(
        "/api/chroma-keyer/previews",
        json={**payload, "view_mode": "alpha"},
    )
    gain_response = client.post(
        "/api/chroma-keyer/previews",
        json={
            **payload,
            "params": {**payload["params"], "screen_gain": 1.3},
        },
    )

    first_url = first_response.json()["preview_url"]
    assert first_response.status_code == 200
    assert duplicate_response.status_code == 200
    assert alpha_response.status_code == 200
    assert gain_response.status_code == 200
    assert duplicate_response.json()["preview_url"] == first_url
    assert alpha_response.json()["preview_url"] != first_url
    assert gain_response.json()["preview_url"] != first_url
    for response in (
        first_response,
        duplicate_response,
        alpha_response,
        gain_response,
    ):
        preview_url = response.json()["preview_url"]
        UUID(preview_url.rsplit("/", maxsplit=1)[1])
        assert str(input_path) not in preview_url


def test_singular_and_plural_preview_routes_share_the_request_cache(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    client = _client(tmp_path)
    payload = {
        "input_path": str(input_path),
        "samples": [[42, 180, 72]],
        "params": {},
    }

    plural_response = client.post("/api/chroma-keyer/previews", json=payload)
    singular_response = client.post("/api/chroma-keyer/preview", json=payload)

    assert plural_response.status_code == 200
    assert singular_response.status_code == 200
    assert (
        singular_response.json()["preview_url"]
        == plural_response.json()["preview_url"]
    )


@mark.parametrize("url", ["/api/chroma-keyer/preview", "/api/chroma-keyer/previews"])
@mark.parametrize(
    "changes",
    [
        {},
        {"screen_gain": 1.4},
        {"screen_balance": 0.2},
        {"clip_black": 0.1},
        {"clip_white": 0.9},
        {"shrink_grow": -1},
        {"feather": 1},
        {"despill_amount": 0.75},
    ],
)
def test_preview_routes_require_nested_params_and_reject_flattened_params(
    tmp_path: Path,
    url: str,
    changes: dict[str, float | int],
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    payload: dict[str, object] = {
        "input_path": str(input_path),
        "samples": [[42, 180, 72]],
        **changes,
    }
    if changes:
        payload["params"] = {}

    response = _client(tmp_path).post(
        url,
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@mark.parametrize("url", ["/api/chroma-keyer/preview", "/api/chroma-keyer/previews"])
@mark.parametrize("samples", [None, [], [[42, 180, 72], [40, 182, 74]]])
def test_preview_schema_rejects_non_single_samples_for_a_valid_image(
    tmp_path: Path,
    url: str,
    samples: list[list[int]] | None,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    payload: dict[str, object] = {
        "input_path": str(input_path),
        "params": {
            "screen_gain": 1.25,
            "screen_balance": 0.35,
            "clip_black": 0.0,
            "clip_white": 1.0,
            "shrink_grow": 0,
            "feather": 0,
            "despill_amount": 0.5,
        },
    }
    if samples is not None:
        payload["samples"] = samples

    response = _client(tmp_path).post(
        url,
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_unknown_preview_returns_structured_not_found(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/chroma-keyer/previews/not-a-preview")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "preview_not_found"


@mark.parametrize(
    "payload",
    [
        {
            "x": 2,
            "y": 0,
            "preview_width": 2,
            "preview_height": 1,
        },
        {
            "x": 0,
            "y": 0,
            "preview_width": 0,
            "preview_height": 1,
        },
    ],
)
def test_invalid_sample_coordinates_return_structured_bad_request(
    tmp_path: Path,
    payload: dict[str, int],
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)

    response = _client(tmp_path).post(
        "/api/chroma-keyer/images/sample",
        json={
            "input_path": str(input_path),
            **payload,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_sample_coordinates"


@mark.parametrize(
    "payload",
    [
        {
            "x": 0,
            "y": 0,
            "preview_width": -0.5,
            "preview_height": 1,
        },
        {
            "x": 400.5,
            "y": 0,
            "preview_width": 400.5,
            "preview_height": 1,
        },
        {
            "x": 0,
            "y": 200.5,
            "preview_width": 400.5,
            "preview_height": 200.5,
        },
    ],
)
def test_sample_rejects_invalid_finite_preview_dimensions_and_bounds(
    tmp_path: Path,
    payload: dict[str, float],
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)

    response = _client(tmp_path).post(
        "/api/chroma-keyer/images/sample",
        json={
            "input_path": str(input_path),
            **payload,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_sample_coordinates"


@mark.parametrize(
    ("url", "payload"),
    [
        ("/api/chroma-keyer/images/inspect", {}),
        (
            "/api/chroma-keyer/images/sample",
            {
                "x": 0,
                "y": 0,
                "preview_width": 1,
                "preview_height": 1,
            },
        ),
        (
            "/api/chroma-keyer/previews",
            {
                "samples": [[42, 180, 72]],
                "params": {},
            },
        ),
    ],
)
def test_chroma_decoder_failures_return_invalid_image(
    tmp_path: Path,
    url: str,
    payload: dict[str, object],
) -> None:
    input_path = tmp_path / "corrupt.png"
    input_path.write_bytes(b"not an image")

    response = _client(tmp_path).post(
        url,
        json={"input_path": str(input_path), **payload},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_image"


@mark.parametrize(
    ("url", "payload"),
    [
        ("/api/chroma-keyer/images/inspect", {}),
        (
            "/api/chroma-keyer/images/sample",
            {
                "x": 0,
                "y": 0,
                "preview_width": 1,
                "preview_height": 1,
            },
        ),
        (
            "/api/chroma-keyer/preview",
            {
                "samples": [[42, 180, 72]],
                "params": {},
            },
        ),
        (
            "/api/chroma-keyer/previews",
            {
                "samples": [[42, 180, 72]],
                "params": {},
            },
        ),
    ],
)
def test_missing_chroma_source_returns_invalid_request(
    tmp_path: Path,
    url: str,
    payload: dict[str, object],
) -> None:
    response = _client(tmp_path).post(
        url,
        json={
            "input_path": str(tmp_path / "missing.png"),
            **payload,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@mark.parametrize(
    ("url", "payload"),
    [
        ("/api/chroma-keyer/images/inspect", {"input_path": "   "}),
        (
            "/api/chroma-keyer/images/sample",
            {
                "input_path": "missing.png",
                "x": 2,
                "y": 0,
                "preview_width": 2,
                "preview_height": 1,
            },
        ),
        (
            "/api/chroma-keyer/previews",
            {
                "input_path": "missing.png",
                "samples": [[42, 180, 72]],
                "params": {},
            },
        ),
    ],
)
def test_invalid_chroma_requests_return_structured_bad_request(
    tmp_path: Path,
    url: str,
    payload: dict[str, object],
) -> None:
    response = _client(tmp_path).post(url, json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_preview_store_evicts_oldest_entry_at_capacity() -> None:
    store = ChromaPreviewStore(max_entries=2)

    first_id = store.put(b"first")
    second_id = store.put(b"second")
    third_id = store.put(b"third")

    UUID(first_id)
    assert store.get(first_id) is None
    assert store.get(second_id) == b"second"
    assert store.get(third_id) == b"third"


def test_preview_store_evicts_reverse_cache_key_mapping() -> None:
    store = ChromaPreviewStore(max_entries=2)

    first_id = store.put(b"first", cache_key="first-request")
    store.put(b"second", cache_key="second-request")
    store.put(b"third", cache_key="third-request")
    replacement_id = store.put(b"replacement", cache_key="first-request")

    assert store.get(first_id) is None
    assert replacement_id != first_id
    assert store.get(replacement_id) == b"replacement"


def test_preview_store_returns_immutable_entry_across_eviction() -> None:
    store = ChromaPreviewStore(max_entries=1)

    def create_unexpected_preview() -> bytes:
        raise AssertionError("cache hit must not create a preview")

    inserted_entry = store.get_or_put_entry("request", lambda: b"first")
    cached_entry = store.get_or_put_entry("request", create_unexpected_preview)
    store.put(b"second")

    assert cached_entry == inserted_entry
    assert store.get(inserted_entry.preview_id) is None
    assert inserted_entry.png_bytes == b"first"


def test_preview_store_releases_global_lock_while_generating_preview() -> None:
    store = ChromaPreviewStore()
    generation_started = Event()
    release_generation = Event()
    blocked_result: list[str] = []
    independent_result: list[tuple[str, bytes | None, str]] = []
    blocked_error: list[BaseException] = []
    independent_complete = Event()

    def create_blocked_preview() -> bytes:
        generation_started.set()
        assert release_generation.wait(timeout=5)
        return b"blocked"

    def request_blocked_preview() -> None:
        try:
            blocked_result.append(
                store.get_or_put("blocked-request", create_blocked_preview)
            )
        except BaseException as exc:
            blocked_error.append(exc)

    def access_independent_cache_entries() -> None:
        try:
            preview_id = store.put(b"independent")
            other_preview_id = store.get_or_put(
                "other-request",
                lambda: b"other",
            )
            independent_result.append(
                (preview_id, store.get(preview_id), other_preview_id)
            )
        finally:
            independent_complete.set()

    blocked_thread = Thread(target=request_blocked_preview)
    blocked_thread.start()
    assert generation_started.wait(timeout=1)
    independent_thread = Thread(target=access_independent_cache_entries)
    independent_thread.start()
    try:
        assert independent_complete.wait(timeout=1)
    finally:
        release_generation.set()
        blocked_thread.join(timeout=5)
        independent_thread.join(timeout=5)

    assert not blocked_thread.is_alive()
    assert not independent_thread.is_alive()
    assert blocked_error == []
    assert len(blocked_result) == 1
    assert len(independent_result) == 1
    assert independent_result[0][1] == b"independent"


def test_preview_store_deduplicates_concurrent_generation_for_the_same_key() -> None:
    store = ChromaPreviewStore()
    generation_started = Event()
    release_generation = Event()
    second_waiter_seen = Event()
    first_result: list[str] = []
    second_result: list[str] = []
    errors: list[BaseException] = []
    generation_calls = 0

    class TrackingInFlightDict(dict):
        def get(self, key, default=None):
            in_flight = super().get(key, default)
            if key == "shared-request" and in_flight is not None:
                second_waiter_seen.set()
            return in_flight

    store._in_flight_by_cache_key = TrackingInFlightDict()

    def create_preview() -> bytes:
        nonlocal generation_calls
        generation_calls += 1
        generation_started.set()
        assert release_generation.wait(timeout=5)
        return b"shared"

    def request_first_preview() -> None:
        try:
            first_result.append(store.get_or_put("shared-request", create_preview))
        except BaseException as exc:
            errors.append(exc)

    def request_second_preview() -> None:
        try:
            second_result.append(
                store.get_or_put(
                    "shared-request",
                    lambda: (_ for _ in ()).throw(
                        AssertionError("duplicate generator must not run")
                    ),
                )
            )
        except BaseException as exc:
            errors.append(exc)

    first_thread = Thread(target=request_first_preview)
    first_thread.start()
    assert generation_started.wait(timeout=1)
    second_thread = Thread(target=request_second_preview)
    second_thread.start()
    assert second_waiter_seen.wait(timeout=1)
    release_generation.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert generation_calls == 1
    assert first_result == second_result


def test_preview_store_releases_same_key_waiters_after_generator_failure() -> None:
    store = ChromaPreviewStore()
    generation_started = Event()
    release_generation = Event()
    second_waiter_seen = Event()
    first_error: list[BaseException] = []
    second_error: list[BaseException] = []
    second_complete = Event()

    class TrackingInFlightDict(dict):
        def get(self, key, default=None):
            in_flight = super().get(key, default)
            if key == "failing-request" and in_flight is not None:
                second_waiter_seen.set()
            return in_flight

    store._in_flight_by_cache_key = TrackingInFlightDict()

    def failing_generator() -> bytes:
        generation_started.set()
        assert release_generation.wait(timeout=5)
        raise RuntimeError("generation failed")

    def request_first_preview() -> None:
        try:
            store.get_or_put("failing-request", failing_generator)
        except BaseException as exc:
            first_error.append(exc)

    def request_second_preview() -> None:
        try:
            store.get_or_put(
                "failing-request",
                lambda: (_ for _ in ()).throw(
                    AssertionError("duplicate generator must not run")
                ),
            )
        except BaseException as exc:
            second_error.append(exc)
        finally:
            second_complete.set()

    first_thread = Thread(target=request_first_preview)
    first_thread.start()
    assert generation_started.wait(timeout=1)
    second_thread = Thread(target=request_second_preview)
    second_thread.start()
    assert second_waiter_seen.wait(timeout=1)
    release_generation.set()
    assert second_complete.wait(timeout=1)
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert [str(error) for error in first_error] == ["generation failed"]
    assert [str(error) for error in second_error] == ["generation failed"]


@mark.parametrize("max_entries", [0, -1, True, 1.5])
def test_preview_store_rejects_invalid_capacity(max_entries: object) -> None:
    with raises(ValueError):
        ChromaPreviewStore(max_entries=max_entries)  # type: ignore[arg-type]


@mark.parametrize("png_bytes", [b"", "not-bytes", bytearray(b"bytes")])
def test_preview_store_rejects_empty_or_non_bytes_values(png_bytes: object) -> None:
    with raises(ValueError):
        ChromaPreviewStore().put(png_bytes)  # type: ignore[arg-type]
