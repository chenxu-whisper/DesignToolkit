from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock
from typing import BinaryIO

from anyio import CancelScope, create_task_group, sleep, to_thread

from designtoolkit.output_names import bounded_derived_name
from designtoolkit.web.hosted import _ALLOWED_SUFFIXES

_CHUNK_BYTES = 1024 * 1024
_RECEIPT = ".ownership.json"


class LocalImportError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def local_import_filename(filename: str) -> str:
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename).rstrip(" .")
    suffix = Path(filename).suffix
    if suffix.lower() not in _ALLOWED_SUFFIXES:
        raise LocalImportError("File type is unsupported")
    stem = filename[: -len(suffix)]
    if re.fullmatch(
        r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]",
        stem.split(".", 1)[0].rstrip(),
        re.IGNORECASE,
    ):
        stem = "_" + stem
    return bounded_derived_name(
        prefix="", stem=stem, suffix="", extension=suffix, max_bytes=120
    )


@dataclass
class _Import:
    path: Path
    identity: tuple[int, int]
    remaining: int
    root_identity: tuple[int, int]
    writer: BinaryIO | None = None
    file_identity: tuple[int, int] | None = None
    receipt_identity: tuple[int, int] | None = None
    active: bool = True
    cancelled: Event = field(default_factory=Event)


def _identity(path: Path) -> tuple[int, int]:
    stat = path.lstat()
    return stat.st_dev, stat.st_ino


class LocalImportStore:
    """Own only directories created here; deleted IDs remain cancellation tombstones."""

    def __init__(
        self, root: Path, *, max_upload_bytes: int, min_free_bytes: int
    ) -> None:
        self.root = root.absolute()
        self.max_upload_bytes = max_upload_bytes
        self.min_free_bytes = min_free_bytes
        self._lock = RLock()
        self._imports: dict[str, _Import | None] = {}

    def _check_space(self, additional: int = 0) -> None:
        reserved = sum(item.remaining for item in self._imports.values() if item)
        if (
            shutil.disk_usage(self.root).free
            < self.min_free_bytes + reserved + additional
        ):
            raise LocalImportError("Insufficient free disk space", 507)

    def _claim(self, import_id: str, filename: str, length: int) -> _Import:
        with self._lock:
            if re.fullmatch(r"[0-9a-f]{32}", import_id) is None:
                raise LocalImportError("Invalid local import ID")
            if filename != local_import_filename(filename):
                raise LocalImportError("Invalid local import filename")
            if import_id in self._imports:
                raise LocalImportError("Local import ID has already been used", 409)
            if length > self.max_upload_bytes:
                raise LocalImportError("File exceeds the configured file limit", 413)
            if self.root.resolve() != self.root:
                raise LocalImportError("Local import directory is not available", 503)
            self.root.mkdir(parents=True, exist_ok=True)
            self._check_space(length)
            directory = self.root / import_id
            try:
                directory.mkdir()
            except FileExistsError as exc:
                raise LocalImportError(
                    "Local import directory already exists", 409
                ) from exc
            item = _Import(
                directory / filename,
                _identity(directory),
                length,
                _identity(self.root),
            )
            self._imports[import_id] = item
            try:
                item.writer = item.path.open("xb", buffering=0)
                item.file_identity = _identity(item.path)
                self._persist(item)
            except BaseException:
                self._finish(item, False)
                raise
            return item

    def _persist(self, item: _Import) -> None:
        receipt = item.path.parent / _RECEIPT
        fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            info = os.fstat(fd)
            item.receipt_identity = (info.st_dev, info.st_ino)
            data = json.dumps(
                {
                    "filename": item.path.name,
                    "root_identity": item.root_identity,
                    "directory_identity": item.identity,
                    "file_identity": item.file_identity,
                    "receipt_identity": item.receipt_identity,
                }
            ).encode("ascii")
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if not written:
                    raise OSError("Local import receipt write made no progress")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)

    def _recover(self, import_id: str) -> _Import | None:
        if re.fullmatch(r"[0-9a-f]{32}", import_id) is None:
            return None
        directory = self.root / import_id
        receipt = directory / _RECEIPT
        try:
            if (
                self.root.resolve() != self.root
                or directory.resolve() != directory
                or receipt.resolve() != receipt
                or not stat.S_ISREG(receipt.lstat().st_mode)
            ):
                return None
            with receipt.open("rb") as reader:
                info = os.fstat(reader.fileno())
                record = json.loads(reader.read(4096))
            if not isinstance(record, dict):
                return None
            filename = record["filename"]
            if not isinstance(filename, str) or filename != local_import_filename(
                filename
            ):
                return None
            identities = []
            for key in (
                "root_identity",
                "directory_identity",
                "file_identity",
                "receipt_identity",
            ):
                value = record[key]
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or any(type(part) is not int or part < 0 for part in value)
                ):
                    return None
                identities.append(tuple(value))
            root_id, directory_id, file_id, receipt_id = identities
            if receipt_id != (info.st_dev, info.st_ino):
                return None
            item = _Import(
                directory / filename,
                directory_id,
                0,
                root_id,
                file_identity=file_id,
                receipt_identity=receipt_id,
                active=False,
            )
            return item if self._owned(item) else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _owned(self, item: _Import) -> bool:
        directory = item.path.parent
        try:
            return (
                self.root.resolve() == self.root
                and _identity(self.root) == item.root_identity
                and directory.parent == self.root
                and directory.resolve() == directory
                and directory.is_dir()
                and _identity(directory) == item.identity
            )
        except OSError:
            return False

    def _write(self, item: _Import, chunk: memoryview) -> None:
        with self._lock:
            if item.cancelled.is_set():
                raise LocalImportError("Local import was cancelled", 409)
            if not self._owned(item):
                raise LocalImportError("Local import ownership was lost", 409)
            if len(chunk) > item.remaining:
                raise LocalImportError("Upload exceeds declared Content-Length")
            self._check_space()
            if item.writer is None:
                item.writer = item.path.open("xb", buffering=0)
                item.file_identity = _identity(item.path)
            offset = 0
            while offset < len(chunk):
                written = item.writer.write(chunk[offset:])
                if not written:
                    raise OSError("Local import write made no progress")
                offset += written
                item.remaining -= written

    def _remove(self, item: _Import) -> None:
        if not self._owned(item):
            return
        receipt = item.path.parent / _RECEIPT
        # Refuse replacement files/links and unexpected children before any unlink.
        for path, identity in (
            (item.path, item.file_identity),
            (receipt, item.receipt_identity),
        ):
            if path.exists() or path.is_symlink():
                if (
                    identity is None
                    or path.resolve() != path
                    or not stat.S_ISREG(path.lstat().st_mode)
                    or _identity(path) != identity
                ):
                    return
        if any(
            child.name not in {item.path.name, _RECEIPT}
            for child in item.path.parent.iterdir()
        ):
            return
        if item.file_identity is not None and item.path.exists():
            item.path.unlink()
        if item.receipt_identity is not None and receipt.exists():
            receipt.unlink()
        # Do not recursively traverse unexpected files, directories or links.
        item.path.parent.rmdir()

    def _finish(self, item: _Import, success: bool) -> None:
        with self._lock:
            try:
                if item.writer is not None:
                    item.writer.close()
            except OSError:
                success = False
                raise
            finally:
                item.writer = None
                item.active = False
                item.remaining = 0
                if not success or item.cancelled.is_set():
                    self._remove(item)

    def delete(self, import_id: str) -> None:
        with self._lock:
            if import_id not in self._imports:
                self._imports[import_id] = self._recover(import_id)
            item = self._imports[import_id]
            if item is None:
                return
            item.cancelled.set()
            # The uploading task closes its writer before removing the directory.
            if not item.active:
                self._remove(item)

    async def store_stream(
        self, import_id: str, filename: str, length: int, chunks: AsyncIterable[bytes]
    ) -> Path:
        item = None
        success = False
        try:
            # Shield claim so cancellation cannot lose a newly owned directory.
            with CancelScope(shield=True):
                item = await to_thread.run_sync(
                    self._claim, import_id, filename, length
                )
            error: BaseException | None = None
            with CancelScope() as scope:
                async with create_task_group() as group:
                    group.start_soon(self._watch_cancellation, item, scope)
                    try:
                        async for chunk in chunks:
                            view = memoryview(chunk)
                            for offset in range(0, len(view), _CHUNK_BYTES):
                                await to_thread.run_sync(
                                    self._write,
                                    item,
                                    view[offset : offset + _CHUNK_BYTES],
                                )
                        if item.remaining:
                            raise LocalImportError(
                                "Upload size does not match declared Content-Length"
                            )
                    except BaseException as exc:
                        error = exc
                    finally:
                        group.cancel_scope.cancel()
            if item.cancelled.is_set():
                raise LocalImportError("Local import was cancelled", 409)
            if error is not None:
                raise error
            success = True
            return item.path
        except OSError as exc:
            raise LocalImportError("Local import storage is unavailable", 503) from exc
        finally:
            if item is not None:
                with CancelScope(shield=True):
                    try:
                        await to_thread.run_sync(self._finish, item, success)
                    except OSError as exc:
                        raise LocalImportError(
                            "Local import storage is unavailable", 503
                        ) from exc

    @staticmethod
    async def _watch_cancellation(item: _Import, scope: CancelScope) -> None:
        while not item.cancelled.is_set():
            await sleep(0.02)
        scope.cancel()
