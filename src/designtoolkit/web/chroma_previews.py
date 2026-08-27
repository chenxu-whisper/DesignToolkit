from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ChromaPreviewEntry:
    """An immutable preview value returned while it is still cache-resident."""

    preview_id: str
    png_bytes: bytes


class ChromaPreviewStore:
    """Thread-safe FIFO store for a bounded number of PNG previews."""

    def __init__(self, max_entries: int = 32) -> None:
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or max_entries <= 0
        ):
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._previews: OrderedDict[str, ChromaPreviewEntry] = OrderedDict()
        self._preview_ids_by_cache_key: dict[str, str] = {}
        self._lock = Lock()

    def put(self, png_bytes: bytes, *, cache_key: str | None = None) -> str:
        if not isinstance(png_bytes, bytes) or not png_bytes:
            raise ValueError("png_bytes must be non-empty bytes")
        if cache_key is not None and (
            not isinstance(cache_key, str) or not cache_key
        ):
            raise ValueError("cache_key must be a non-empty string")

        with self._lock:
            if cache_key is not None:
                existing_preview_id = self._preview_ids_by_cache_key.get(cache_key)
                if existing_preview_id in self._previews:
                    return existing_preview_id
                self._preview_ids_by_cache_key.pop(cache_key, None)

            return self._insert_locked(png_bytes, cache_key=cache_key).preview_id

    def get_or_put(self, cache_key: str, create_png_bytes) -> str:
        return self.get_or_put_entry(cache_key, create_png_bytes).preview_id

    def get_or_put_entry(
        self,
        cache_key: str,
        create_png_bytes,
    ) -> ChromaPreviewEntry:
        if not isinstance(cache_key, str) or not cache_key:
            raise ValueError("cache_key must be a non-empty string")

        with self._lock:
            existing_preview_id = self._preview_ids_by_cache_key.get(cache_key)
            existing_entry = self._previews.get(existing_preview_id)
            if existing_entry is not None:
                return existing_entry
            self._preview_ids_by_cache_key.pop(cache_key, None)

            png_bytes = create_png_bytes()
            if not isinstance(png_bytes, bytes) or not png_bytes:
                raise ValueError("png_bytes must be non-empty bytes")

            return self._insert_locked(png_bytes, cache_key=cache_key)

    def get(self, preview_id: str) -> bytes | None:
        with self._lock:
            entry = self._previews.get(preview_id)
            return None if entry is None else entry.png_bytes

    def _insert_locked(
        self,
        png_bytes: bytes,
        *,
        cache_key: str | None,
    ) -> ChromaPreviewEntry:
        entry = ChromaPreviewEntry(preview_id=str(uuid4()), png_bytes=png_bytes)
        self._previews[entry.preview_id] = entry
        if cache_key is not None:
            self._preview_ids_by_cache_key[cache_key] = entry.preview_id
        if len(self._previews) > self._max_entries:
            evicted_preview_id, _ = self._previews.popitem(last=False)
            for key, mapped_preview_id in self._preview_ids_by_cache_key.items():
                if mapped_preview_id == evicted_preview_id:
                    del self._preview_ids_by_cache_key[key]
                    break
        return entry
