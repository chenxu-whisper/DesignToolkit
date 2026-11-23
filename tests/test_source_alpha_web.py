import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import ValidationError

from designtoolkit.chroma_keyer.models import KeyerParams
from designtoolkit.web.adapters import chroma_video_keyer_options_from_request
from designtoolkit.web.app import create_app
from designtoolkit.web.logs import LogStore
from designtoolkit.web.packed_video import hosted_video_request
from designtoolkit.web.schemas import (
    ChromaKeyerTaskRequest,
    ChromaPreviewRequest,
    ChromaVideoKeyerTaskRequest,
    ChromaVideoPreviewRequest,
)
from designtoolkit.web.tasks import TaskManager
from test_source_alpha_export import make_native


def source_payload(path="native.mov", mode="straight"):
    return {
        "input_path": str(path),
        "alpha_source": "source",
        "source_alpha_mode": mode,
        "output_format": "packed_alpha_mp4",
    }


def test_source_task_defaults_and_adapter_bypass_sampling():
    request = ChromaVideoKeyerTaskRequest(**source_payload())
    assert request.samples == []
    assert (
        request.params.model_dump()
        == ChromaVideoKeyerTaskRequest(
            input_path="native.mov", samples=[[0, 255, 0]], params={}
        ).params.model_dump()
    )
    options, model, params = chroma_video_keyer_options_from_request(request)
    assert options.alpha_source == "source"
    assert options.source_alpha_mode == "straight"
    assert model is None
    assert params == KeyerParams()


def test_source_preview_defaults_without_samples_or_params():
    request = ChromaVideoPreviewRequest(
        input_path="native.mov", alpha_source="source", timestamp_seconds=0.0
    )
    assert request.samples == []
    assert request.source_alpha_mode == "straight"


@pytest.mark.parametrize(
    "request_type, extra",
    [
        (ChromaVideoKeyerTaskRequest, {}),
        (ChromaVideoPreviewRequest, {"timestamp_seconds": 0.0}),
    ],
)
@pytest.mark.parametrize("samples", [None, [], [[0, 255, 0], [0, 255, 0]], [[0, 255]]])
def test_chroma_still_requires_exactly_one_sample(request_type, extra, samples):
    payload = {"input_path": "native.mov", "params": {}, **extra}
    if samples is not None:
        payload["samples"] = samples
    with pytest.raises(ValidationError):
        request_type(**payload)


@pytest.mark.parametrize(
    "fields",
    [
        {"alpha_source": "guess"},
        {"source_alpha_mode": "guess"},
        {"output_format": "png_sequence"},
        {"samples": [[0, 255]]},
        {"samples": [[0, 255, 0], [0, 255, 0]]},
    ],
)
def test_source_task_rejects_invalid_contract(fields):
    with pytest.raises(ValidationError):
        ChromaVideoKeyerTaskRequest(**{**source_payload(), **fields})


@pytest.mark.parametrize(
    "fields",
    [
        {"alpha_source": "guess"},
        {"source_alpha_mode": "guess"},
    ],
)
def test_source_preview_rejects_unknown_modes(fields):
    with pytest.raises(ValidationError):
        ChromaVideoPreviewRequest(
            **{
                "input_path": "native.mov",
                "alpha_source": "source",
                "timestamp_seconds": 0.0,
                **fields,
            }
        )


def test_legacy_schema_defaults_and_image_contract_unchanged():
    request = ChromaVideoKeyerTaskRequest(
        input_path="native.mov", samples=[[0, 255, 0]], params={}
    )
    assert request.alpha_source == "chroma_key"
    assert request.source_alpha_mode == "straight"
    assert request.output_format == "png_sequence"
    for request_type in (ChromaKeyerTaskRequest, ChromaPreviewRequest):
        with pytest.raises(ValidationError):
            request_type(input_path="image.png", alpha_source="source")


def test_hosted_and_worker_roundtrip_preserves_source_fields(tmp_path):
    from designtoolkit.web.worker import _task_request

    request = ChromaVideoKeyerTaskRequest(
        **source_payload(mode="premultiplied"),
        output_root="foreign",
        output_path="foreign.mp4",
    )
    owned = tmp_path / "owned"
    safe = hosted_video_request(request, tmp_path / "asset.mov", owned)
    assert safe.input_path == str(tmp_path / "asset.mov")
    assert Path(safe.output_path).parent == owned
    restored = _task_request("chroma_video_keyer", safe.model_dump(mode="json"))
    options, model, _ = chroma_video_keyer_options_from_request(restored)
    assert model is None
    assert options.source_alpha_mode == "premultiplied"
    assert options.alpha_source == "source"


def test_actual_source_task_through_http_exports_without_samples(tmp_path):
    path, _ = make_native(tmp_path / "native.mov", audio=True)
    manager = TaskManager(log_store=LogStore(), run_async=False)
    app = create_app(manager, local_path_roots=[tmp_path])

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/tasks/chroma-keyer-video", json=source_payload(path)
            )
            assert response.status_code == 202, response.text
            status = await client.get(f"/api/tasks/{response.json()['task_id']}")
            assert status.json()["status"] == "succeeded", status.text
            assert (tmp_path / "native_keyed_packed.mp4").exists()
            assert (tmp_path / "native_keyed_packed_player/index.html").exists()

    asyncio.run(exercise())


def test_preview_png_preserves_alpha_and_isolates_modes_and_chroma_cache(tmp_path):
    path, frame = make_native(tmp_path / "native.mov")
    app = create_app(TaskManager(log_store=LogStore()), local_path_roots=[tmp_path])

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:

            async def preview(mode="straight", view="final", source="source"):
                payload = {
                    "input_path": str(path),
                    "timestamp_seconds": 0.3,
                    "alpha_source": source,
                    "source_alpha_mode": mode,
                    "samples": [[0, 255, 0]],
                    "params": {},
                    "view_mode": view,
                }
                response = await client.post(
                    "/api/chroma-keyer/videos/preview", json=payload
                )
                assert response.status_code == 200, response.text
                url = response.json()["preview_url"]
                png = await client.get(url)
                assert png.status_code == 200
                return url, np.asarray(Image.open(BytesIO(png.content)))

            straight_url, straight = await preview()
            again, _ = await preview()
            assert again == straight_url
            np.testing.assert_array_equal(straight[..., 3], frame[..., 3])
            np.testing.assert_array_equal(straight[8, 12, :3], [20, 220, 40])
            premult_url, premult = await preview("premultiplied")
            assert premult_url != straight_url
            np.testing.assert_array_equal(premult[8, 12], [40, 255, 80, 128])
            np.testing.assert_array_equal(premult[8, 4], [0, 0, 0, 0])
            chroma_url, chroma = await preview(source="chroma_key")
            assert chroma_url not in (straight_url, premult_url)
            assert not np.array_equal(chroma[..., 3], straight[..., 3])
            alpha_url, alpha = await preview(view="alpha")
            assert alpha_url != straight_url
            assert alpha.shape == (16, 32, 3)
            np.testing.assert_array_equal(alpha[..., 0], frame[..., 3])

    asyncio.run(exercise())


def test_missing_alpha_preview_and_task_fail_without_outputs(tmp_path):
    path, _ = make_native(tmp_path / "rgb.mov", "libx264")
    manager = TaskManager(log_store=LogStore(), run_async=False)
    app = create_app(manager, local_path_roots=[tmp_path])

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            preview = await client.post(
                "/api/chroma-keyer/videos/preview",
                json={
                    "input_path": str(path),
                    "alpha_source": "source",
                    "timestamp_seconds": 0.0,
                },
            )
            assert preview.status_code == 400, preview.text
            assert preview.json()["error"]["code"] == "invalid_video"
            assert "native alpha" in preview.text.lower()
            task = await client.post(
                "/api/tasks/chroma-keyer-video", json=source_payload(path)
            )
            assert task.status_code == 202, task.text
            status = await client.get(f"/api/tasks/{task.json()['task_id']}")
            assert status.json()["status"] == "failed"
            assert "native alpha" in status.json()["error"].lower()

    asyncio.run(exercise())
    assert [p.name for p in tmp_path.iterdir()] == ["rgb.mov"]


def test_hosted_source_preview_uses_owned_asset_pin_and_owner_store(
    tmp_path, monkeypatch
):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    import designtoolkit.web.routes as routes
    from designtoolkit.web.hosted import (
        HostedSettings,
        HostedStorage,
        _write_asset_manifest,
    )
    from designtoolkit.web.redis_tasks import RedisTaskManager
    from designtoolkit.web.routes import ApiError, create_router

    owner, other, asset_id = "a" * 32, "b" * 32, "c" * 32
    root = tmp_path / "hosted"
    asset_dir = root / "sessions" / owner / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    path, frame = make_native(asset_dir / "native.mov")
    _write_asset_manifest(
        asset_dir, asset_id=asset_id, generation_id=None, size=path.stat().st_size
    )
    storage = HostedStorage(root, max_upload_bytes=1000000)
    events = []
    active_owners = []
    original_read = routes.read_source_preview

    def pinned_read(*args):
        assert owner in active_owners
        events.append(("decode", owner))
        return original_read(*args)

    monkeypatch.setattr(routes, "read_source_preview", pinned_read)

    class PinnedManager(RedisTaskManager):
        def __init__(self):
            self.log_store = LogStore()
            self.asset_read_pin_heartbeat_interval = 5
            self.heartbeat_stop_timeout_seconds = 1

        def acquire_asset_read_pin(self, session_id, identities, *, ttl_seconds):
            events.append(("pin", session_id, identities))
            active_owners.append(session_id)

            def release():
                active_owners.remove(session_id)
                events.append(("release", session_id))

            return SimpleNamespace(
                refresh=lambda: events.append(("refresh", session_id)),
                release=release,
            )

    app = FastAPI()

    @app.middleware("http")
    async def set_owner(request: Request, call_next):
        request.state.session_id = request.headers.get("x-owner", owner)
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def api_error(request, error):
        return JSONResponse({"error": error.code}, status_code=error.status_code)

    app.include_router(
        create_router(
            PinnedManager(),
            hosted_storage=storage,
            hosted_settings=HostedSettings(
                True, root, "redis://unused", min_free_bytes=0
            ),
        )
    )

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "input_path": f"asset:{asset_id}",
                "alpha_source": "source",
                "timestamp_seconds": 0.0,
            }
            response = await client.post(
                "/api/chroma-keyer/videos/preview", json=payload
            )
            assert response.status_code == 200, response.text
            url = response.json()["preview_url"]
            png = await client.get(url)
            pixels = np.asarray(Image.open(BytesIO(png.content)))
            np.testing.assert_array_equal(pixels[..., 3], frame[..., 3])
            foreign_png = await client.get(url, headers={"x-owner": other})
            assert foreign_png.status_code == 404
            foreign_source = await client.post(
                "/api/chroma-keyer/videos/preview",
                json=payload,
                headers={"x-owner": other},
            )
            assert foreign_source.status_code == 404

    asyncio.run(exercise())
    assert ("pin", owner, ((asset_id, None),)) in events
    assert ("decode", owner) in events
    assert events.count(("release", owner)) == 1
    assert events.count(("release", other)) == 1
    assert active_owners == []


def test_source_preview_retries_changed_snapshot_without_stale_cache(
    tmp_path, monkeypatch
):
    import designtoolkit.web.routes as routes

    path, _ = make_native(tmp_path / "native.mov")
    replacement, _ = make_native(tmp_path / "replacement.mov", opaque=True)
    original = routes.read_source_preview
    calls = []

    def change_after_read(path, timestamp, mode):
        value = original(path, timestamp, mode)
        calls.append(True)
        if len(calls) == 1:
            path.write_bytes(replacement.read_bytes())
        return value

    monkeypatch.setattr(routes, "read_source_preview", change_after_read)
    app = create_app(TaskManager(log_store=LogStore()), local_path_roots=[tmp_path])

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/chroma-keyer/videos/preview",
                json={
                    "input_path": str(path),
                    "alpha_source": "source",
                    "timestamp_seconds": 0.0,
                },
            )
            assert response.status_code == 200, response.text
            png = await client.get(response.json()["preview_url"])
            pixels = np.asarray(Image.open(BytesIO(png.content)))
            assert np.all(pixels[..., 3] == 255)

    asyncio.run(exercise())
    assert len(calls) == 2
