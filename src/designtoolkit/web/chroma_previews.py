from collections import OrderedDict
from threading import Lock
from uuid import uuid4


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
        self._previews: OrderedDict[str, bytes] = OrderedDict()
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

            preview_id = str(uuid4())
            self._previews[preview_id] = png_bytes
            if cache_key is not None:
                self._preview_ids_by_cache_key[cache_key] = preview_id
            if len(self._previews) > self._max_entries:
                evicted_preview_id, _ = self._previews.popitem(last=False)
                for key, mapped_preview_id in self._preview_ids_by_cache_key.items():
                    if mapped_preview_id == evicted_preview_id:
                        del self._preview_ids_by_cache_key[key]
                        break
        return preview_id

    def get_or_put(self, cache_key: str, create_png_bytes) -> str:
        if not isinstance(cache_key, str) or not cache_key:
            raise ValueError("cache_key must be a non-empty string")

        with self._lock:
            existing_preview_id = self._preview_ids_by_cache_key.get(cache_key)
            if existing_preview_id in self._previews:
                return existing_preview_id
            self._preview_ids_by_cache_key.pop(cache_key, None)

            png_bytes = create_png_bytes()
            if not isinstance(png_bytes, bytes) or not png_bytes:
                raise ValueError("png_bytes must be non-empty bytes")

            preview_id = str(uuid4())
            self._previews[preview_id] = png_bytes
            self._preview_ids_by_cache_key[cache_key] = preview_id
            if len(self._previews) > self._max_entries:
                evicted_preview_id, _ = self._previews.popitem(last=False)
                for key, mapped_preview_id in self._preview_ids_by_cache_key.items():
                    if mapped_preview_id == evicted_preview_id:
                        del self._preview_ids_by_cache_key[key]
                        break
            return preview_id

    def get(self, preview_id: str) -> bytes | None:
        with self._lock:
            return self._previews.get(preview_id)
