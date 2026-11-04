from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from designtoolkit.web.app import create_app
from designtoolkit.web.hosted import HostedSettings
from designtoolkit.web.local_imports import LocalImportError, LocalImportStore
from designtoolkit.web.logs import LogStore
from designtoolkit.web.tasks import TaskManager

URL = "/api/local-paths/import-files"
IMPORT_ID = "a" * 32
RECEIPT = ".ownership.json"


def import_store(root):
    return LocalImportStore(root, max_upload_bytes=1024, min_free_bytes=0)


async def import_bytes(store, import_id=IMPORT_ID):
    async def chunks():
        yield b"abc"

    return await store.store_stream(import_id, "sample.png", 3, chunks())


def test_fresh_store_deletes_completed_import_after_response_loss(tmp_path):
    root = tmp_path / "imports"
    path = asyncio.run(import_bytes(import_store(root)))
    fresh = import_store(root)
    fresh.delete(IMPORT_ID)
    fresh.delete(IMPORT_ID)
    assert not path.parent.exists()


def test_fresh_store_deletes_interrupted_import(tmp_path):
    root = tmp_path / "imports"
    original = import_store(root)
    item = original._claim(IMPORT_ID, "sample.png", 3)
    original._write(item, memoryview(b"a"))
    item.writer.close()
    item.writer = None
    import_store(root).delete(IMPORT_ID)
    assert not item.path.parent.exists()


@pytest.mark.parametrize("replacement", ["directory", "file", "root", "foreign"])
def test_fresh_store_preserves_unowned_replacements(tmp_path, replacement):
    root = tmp_path / "imports"
    path = asyncio.run(import_bytes(import_store(root)))
    if replacement == "directory":
        path.parent.rename(root / "moved")
        path.parent.mkdir()
        path.write_bytes(b"foreign")
    elif replacement == "file":
        path.rename(path.parent / "moved.png")
        path.write_bytes(b"foreign")
    elif replacement == "root":
        root.rename(tmp_path / "moved")
        root.mkdir()
        (tmp_path / "moved" / IMPORT_ID).rename(root / IMPORT_ID)
    else:
        path = root / ("b" * 32) / "sample.png"
        path.parent.mkdir()
        path.write_bytes(b"foreign")
    before = path.read_bytes()
    import_store(root).delete(path.parent.name)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "filename", ["../outside.png", r"..\outside.png", "C:outside.png", "/outside.png"]
)
def test_fresh_store_rejects_receipt_path_escape(tmp_path, filename):
    root = tmp_path / "imports"
    path = asyncio.run(import_bytes(import_store(root)))
    receipt = path.parent / RECEIPT
    record = json.loads(receipt.read_text())
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"foreign")
    record["filename"] = filename
    record["file_identity"] = [outside.stat().st_dev, outside.stat().st_ino]
    receipt.write_text(json.dumps(record))
    import_store(root).delete(IMPORT_ID)
    assert outside.read_bytes() == b"foreign"
    assert path.read_bytes() == b"abc"


@pytest.mark.parametrize("replacement", ["receipt", "file", "directory"])
def test_fresh_store_rejects_symlinks(tmp_path, replacement):
    root = tmp_path / "imports"
    path = asyncio.run(import_bytes(import_store(root)))
    target = path.parent / RECEIPT if replacement == "receipt" else path
    if replacement == "directory":
        target = path.parent
    moved = tmp_path / "moved"
    target.rename(moved)
    try:
        target.symlink_to(moved, target_is_directory=replacement == "directory")
    except OSError:
        moved.rename(target)
        pytest.skip("Symlink creation requires platform permission")
    import_store(root).delete(IMPORT_ID)
    assert target.is_symlink()
    assert moved.exists()


def test_receipt_short_writes_are_completed_before_stream_is_consumed(
    tmp_path, monkeypatch
):
    root = tmp_path / "imports"
    write = os.write
    calls = 0

    def short_write(fd, data):
        nonlocal calls
        calls += 1
        return write(fd, data[:7])

    monkeypatch.setattr(os, "write", short_write)

    async def run():
        async def chunks():
            record = json.loads((root / IMPORT_ID / RECEIPT).read_text())
            assert record["filename"] == "sample.png"
            yield b"abc"

        return await import_store(root).store_stream(
            IMPORT_ID, "sample.png", 3, chunks()
        )

    path = asyncio.run(run())
    assert calls > 1
    import_store(root).delete(IMPORT_ID)
    assert not path.parent.exists()


@pytest.mark.parametrize("failure", ["write", "no_progress", "fsync"])
def test_receipt_persistence_failure_cleans_claim(tmp_path, monkeypatch, failure):
    root = tmp_path / "imports"

    def fail(*_):
        if failure == "no_progress":
            return 0
        raise OSError("receipt storage failed")

    monkeypatch.setattr(os, "fsync" if failure == "fsync" else "write", fail)
    with pytest.raises(LocalImportError, match="storage is unavailable"):
        asyncio.run(import_bytes(import_store(root)))
    assert not (root / IMPORT_ID).exists()


@pytest.mark.parametrize(
    "change", ["invalid_json", "invalid_identity", "receipt_replaced", "extra_child"]
)
def test_fresh_store_preserves_import_with_invalid_receipt_or_contents(
    tmp_path, change
):
    root = tmp_path / "imports"
    path = asyncio.run(import_bytes(import_store(root)))
    receipt = path.parent / RECEIPT
    if change == "invalid_json":
        receipt.write_bytes(b"{")
    elif change == "invalid_identity":
        record = json.loads(receipt.read_text())
        record["file_identity"] = ["invalid", 1]
        receipt.write_text(json.dumps(record))
    elif change == "receipt_replaced":
        data = receipt.read_bytes()
        receipt.rename(tmp_path / "old-receipt")
        receipt.write_bytes(data)
    else:
        (path.parent / "foreign").mkdir()
    import_store(root).delete(IMPORT_ID)
    assert path.read_bytes() == b"abc"
    assert receipt.exists()


def test_writer_close_failure_cleans_copy_and_reports_storage_error(
    tmp_path, monkeypatch
):
    root = tmp_path / "imports"
    original_open = Path.open

    class FailingClose:
        def __init__(self, writer):
            self.writer = writer

        def write(self, chunk):
            return self.writer.write(chunk)

        def close(self):
            self.writer.close()
            raise OSError("close failed")

    def failing_open(path, mode="r", *args, **kwargs):
        writer = original_open(path, mode, *args, **kwargs)
        return FailingClose(writer) if mode == "xb" else writer

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(LocalImportError, match="storage is unavailable"):
        asyncio.run(import_bytes(import_store(root)))
    assert not (root / IMPORT_ID).exists()


def test_delete_preserves_copy_when_root_disappears(tmp_path):
    root = tmp_path / "imports"
    store = import_store(root)
    asyncio.run(import_bytes(store))
    moved = root.rename(tmp_path / "moved")
    store.delete(IMPORT_ID)
    assert (moved / IMPORT_ID / "sample.png").read_bytes() == b"abc"


def headers(filename="sample.png", size=3, import_id=IMPORT_ID):
    return {
        "X-Local-Import-Id": import_id,
        "X-Upload-Filename": quote(filename, safe=""),
        "Content-Length": str(size),
    }


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = replace(
        HostedSettings.from_env({}), max_upload_bytes=1024 * 1024, min_free_bytes=0
    )
    return create_app(TaskManager(log_store=LogStore()), hosted_settings=settings)


def test_content_only_file_import_uses_actual_bytes_not_same_name_lookup(app, tmp_path):
    original = tmp_path / "sample.png"
    original.write_bytes(b"unrelated original")
    image = BytesIO()
    Image.new("RGB", (3, 2), "green").save(image, format="PNG")
    data = image.getvalue()
    with TestClient(app) as client:
        response = client.post(URL, headers=headers(size=len(data)), content=data)
        assert response.status_code == 201
        body = response.json()
        path = Path(body["path"])
        assert path == tmp_path / ".temp/local-imports" / IMPORT_ID / "sample.png"
        assert path.is_absolute()
        assert path.read_bytes() == data
        assert body == {"path": str(path), "filename": "sample.png", "size": len(data)}
        inspected = client.post(
            "/api/chroma-keyer/images/inspect", json={"input_path": str(path)}
        )
        assert inspected.status_code == 200
        assert inspected.json()["width"] == 3
    assert original.read_bytes() == b"unrelated original"


@pytest.mark.parametrize(
    "filename",
    [
        "../../sample.png",
        r"C:\outside\sample.png",
        "CON.png",
        "CON.extra.png",
        "NUL .png",
        "x:y?.MP4",
        "long" * 200 + ".png",
        "\u56fe" * 300 + ".png",
    ],
)
def test_names_are_portable_bounded_and_same_names_are_separated(app, filename):
    with TestClient(app) as client:
        first = client.post(URL, headers=headers(filename), content=b"one")
        second = client.post(
            URL, headers=headers(filename, import_id="b" * 32), content=b"two"
        )
        assert first.status_code == second.status_code == 201
        first_path, second_path = (
            Path(first.json()["path"]),
            Path(second.json()["path"]),
        )
        assert first_path.parent.name == IMPORT_ID
        assert second_path.parent.name == "b" * 32
        assert first_path.name == second_path.name
        assert len(first_path.name.encode("utf-8")) <= 120
        assert not any(c in first_path.name for c in '<>:"/\\|?*')
        assert first_path.name.split(".", 1)[0].rstrip().upper() not in {"CON", "NUL"}
        assert first_path.read_bytes() == b"one"
        assert second_path.read_bytes() == b"two"


def test_duplicate_id_cannot_overwrite_and_delete_only_removes_owned_import(
    app, tmp_path
):
    unrelated = tmp_path / ".temp/local-imports" / ("c" * 32)
    unrelated.mkdir(parents=True)
    (unrelated / "original.png").write_bytes(b"original")
    with TestClient(app) as client:
        first = client.post(URL, headers=headers(), content=b"one")
        assert first.status_code == 201
        path = Path(first.json()["path"])
        retry = client.post(URL, headers=headers(), content=b"two")
        assert retry.status_code == 409
        assert path.read_bytes() == b"one"
        # Cleanup by ID works even if the caller lost the POST response.
        assert client.delete(f"{URL}/{IMPORT_ID}").status_code == 204
        assert not path.parent.exists()
        assert client.delete(f"{URL}/{IMPORT_ID}").status_code == 204
        assert client.post(URL, headers=headers(), content=b"two").status_code == 409
        assert client.delete(f"{URL}/{'c' * 32}").status_code == 204
        assert (unrelated / "original.png").read_bytes() == b"original"
        assert (
            client.post(
                URL, headers=headers(import_id="c" * 32), content=b"two"
            ).status_code
            == 409
        )
        assert client.delete(f"{URL}/invalid").status_code == 404


@pytest.mark.parametrize(
    ("changes", "data", "expected"),
    [
        ({"X-Local-Import-Id": "invalid"}, b"abc", 400),
        ({"X-Local-Import-Id": None}, b"abc", 400),
        ({"X-Upload-Filename": None}, b"abc", 400),
        ({"X-Upload-Filename": "%ZZ.png"}, b"abc", 400),
        ({"X-Upload-Filename": "%FF.png"}, b"abc", 400),
        ({"X-Upload-Filename": "%00.png"}, b"abc", 400),
        ({"X-Upload-Filename": ".png"}, b"abc", 400),
        ({"X-Upload-Filename": "sample.exe"}, b"abc", 400),
        ({"Content-Length": "bad"}, b"abc", 411),
        ({"Content-Length": "-1"}, b"abc", 411),
        ({"Content-Length": "0"}, b"", 411),
        ({"Content-Length": "4"}, b"abc", 400),
        ({"Content-Length": "2"}, b"abc", 400),
        ({"Content-Length": "3"}, b"", 400),
        ({"Content-Length": str(1024 * 1024 + 1)}, b"abc", 413),
    ],
)
def test_invalid_uploads_leave_no_partial_files(app, tmp_path, changes, data, expected):
    upload_headers = headers()
    upload_headers.update(changes)
    upload_headers = {k: v for k, v in upload_headers.items() if v is not None}
    with TestClient(app) as client:
        response = client.post(URL, headers=upload_headers, content=data)
        assert response.status_code == expected
    assert not (tmp_path / ".temp/local-imports" / IMPORT_ID).exists()


def test_local_interfaces_are_hidden_in_hosted_mode(tmp_path):
    settings = replace(
        HostedSettings.from_env({}), enabled=True, data_dir=tmp_path / "hosted"
    )
    hosted = create_app(
        TaskManager(log_store=LogStore()),
        hosted_settings=settings,
        resource_coordinator=object(),
    )
    with TestClient(hosted) as client:
        assert client.post(URL, headers=headers(), content=b"abc").status_code == 404
        assert client.delete(f"{URL}/{IMPORT_ID}").status_code == 404
    assert not (tmp_path / ".temp/local-imports").exists()


def test_disk_space_limit_is_checked_before_and_during_stream(
    app, tmp_path, monkeypatch
):
    # Patch the operating-system boundary, not the import implementation.
    import shutil

    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(10, 10, 0)
    )
    with TestClient(app) as client:
        assert client.post(URL, headers=headers(), content=b"abc").status_code == 507
    assert not (tmp_path / ".temp/local-imports" / IMPORT_ID).exists()


def test_short_disk_writes_are_completed_before_success(app, monkeypatch):
    original_open = Path.open

    class ShortWriter:
        def __init__(self, writer):
            self.writer = writer

        def write(self, chunk):
            return self.writer.write(chunk[:1])

        def close(self):
            self.writer.close()

    def short_open(path, mode="r", *args, **kwargs):
        writer = original_open(path, mode, *args, **kwargs)
        return ShortWriter(writer) if mode == "xb" else writer

    monkeypatch.setattr(Path, "open", short_open)
    with TestClient(app) as client:
        response = client.post(URL, headers=headers(), content=b"abc")
        assert response.status_code == 201
        assert Path(response.json()["path"]).read_bytes() == b"abc"


def test_delete_before_post_prevents_delayed_import(app, tmp_path):
    with TestClient(app) as client:
        assert client.delete(f"{URL}/{IMPORT_ID}").status_code == 204
        assert client.post(URL, headers=headers(), content=b"abc").status_code == 409
    assert not (tmp_path / ".temp/local-imports" / IMPORT_ID).exists()


def test_configured_free_space_floor_is_preserved(app, monkeypatch):
    import shutil

    settings = replace(
        HostedSettings.from_env({}), max_upload_bytes=1024, min_free_bytes=10
    )
    local_app = create_app(TaskManager(log_store=LogStore()), hosted_settings=settings)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 89, 11)
    )
    with TestClient(local_app) as client:
        assert client.post(URL, headers=headers(), content=b"abc").status_code == 507


def test_delete_never_removes_replacement_directory(app, tmp_path):
    with TestClient(app) as client:
        response = client.post(URL, headers=headers(), content=b"abc")
        assert response.status_code == 201
        directory = Path(response.json()["path"]).parent
        directory.rename(tmp_path / "moved-import")
        directory.mkdir()
        original = directory / "sample.png"
        original.write_bytes(b"replacement")
        assert client.delete(f"{URL}/{IMPORT_ID}").status_code == 204
        assert original.read_bytes() == b"replacement"


def test_disk_space_exhaustion_mid_stream_cleans_partial_file(
    app, tmp_path, monkeypatch
):
    import shutil

    checks = 0

    def disk_usage(_):
        nonlocal checks
        checks += 1
        return shutil._ntuple_diskusage(100, 0, 100 if checks <= 2 else 0)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)

    async def run():
        messages = iter(
            [
                {"type": "http.request", "body": b"a", "more_body": True},
                {"type": "http.request", "body": b"bc", "more_body": False},
            ]
        )
        sent = []

        async def receive():
            return next(messages)

        await asgi_upload(app, receive, sent)
        assert sent[0]["status"] == 507

    asyncio.run(run())
    assert checks == 3
    assert not (tmp_path / ".temp/local-imports" / IMPORT_ID).exists()


async def asgi_upload(app, receive, sent):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": URL,
        "raw_path": URL.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 1234),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers().items()],
    }

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)


def test_disconnect_cleans_partial_file(app, tmp_path):
    async def run():
        messages = iter(
            [
                {"type": "http.request", "body": b"a", "more_body": True},
                {"type": "http.disconnect"},
            ]
        )
        sent = []

        async def receive():
            return next(messages)

        await asgi_upload(app, receive, sent)
        assert sent[0]["status"] == 400

    asyncio.run(run())
    assert not (tmp_path / ".temp/local-imports" / IMPORT_ID).exists()


def test_delete_cancels_active_upload_without_waiting_for_more_body(app, tmp_path):
    async def run():
        paused = asyncio.Event()
        sent = []
        calls = 0

        async def receive():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"type": "http.request", "body": b"a", "more_body": True}
            paused.set()
            await asyncio.Event().wait()

        upload = asyncio.create_task(asgi_upload(app, receive, sent))
        try:
            await asyncio.wait_for(paused.wait(), timeout=1)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                assert (await client.delete(f"{URL}/{IMPORT_ID}")).status_code == 204
                assert (
                    await client.post(URL, headers=headers(), content=b"two")
                ).status_code == 409
                other = await client.post(
                    URL, headers=headers(import_id="b" * 32), content=b"two"
                )
                assert other.status_code == 201
                assert Path(other.json()["path"]).read_bytes() == b"two"
            await asyncio.wait_for(upload, timeout=2)
            assert sent[0]["status"] == 409
            assert not (tmp_path / ".temp/local-imports" / IMPORT_ID).exists()
        finally:
            if not upload.done():
                upload.cancel()
            await asyncio.gather(upload, return_exceptions=True)

    asyncio.run(run())
