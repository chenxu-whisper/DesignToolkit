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
        self._lock = Lock()

    def put(self, png_bytes: bytes) -> str:
        if not isinstance(png_bytes, bytes) or not png_bytes:
            raise ValueError("png_bytes must be non-empty bytes")

        preview_id = str(uuid4())
        with self._lock:
            self._previews[preview_id] = png_bytes
            if len(self._previews) > self._max_entries:
                self._previews.popitem(last=False)
        return preview_id

    def get(self, preview_id: str) -> bytes | None:
        with self._lock:
            return self._previews.get(preview_id)
