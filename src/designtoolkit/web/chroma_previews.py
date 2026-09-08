from collections import OrderedDict
from dataclasses import dataclass
from threading import Event, Lock
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ChromaPreviewEntry:
    """An immutable preview value returned while it is still cache-resident."""

    preview_id: str
    png_bytes: bytes
    owner_id: str | None = None


@dataclass(slots=True)
class _InFlightPreview:
    completed: Event
    entry: ChromaPreviewEntry | None = None
    error: BaseException | None = None


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
        self._preview_ids_by_cache_key: dict[tuple[str | None, str], str] = {}
        self._in_flight_by_cache_key: dict[
            tuple[str | None, str],
            _InFlightPreview,
        ] = {}
        self._lock = Lock()

    def put(
        self,
        png_bytes: bytes,
        *,
        cache_key: str | None = None,
        owner_id: str | None = None,
    ) -> str:
        if not isinstance(png_bytes, bytes) or not png_bytes:
            raise ValueError("png_bytes must be non-empty bytes")
        if cache_key is not None and (
            not isinstance(cache_key, str) or not cache_key
        ):
            raise ValueError("cache_key must be a non-empty string")

        with self._lock:
            if cache_key is not None:
                cache_identity = (owner_id, cache_key)
                existing_entry = self._entry_for_cache_key_locked(cache_identity)
                if existing_entry is not None:
                    return existing_entry.preview_id
            else:
                cache_identity = None

            return self._insert_locked(
                png_bytes,
                cache_identity=cache_identity,
                owner_id=owner_id,
            ).preview_id

    def get_or_put(
        self,
        cache_key: str,
        create_png_bytes,
        *,
        owner_id: str | None = None,
    ) -> str:
        return self.get_or_put_entry(
            cache_key,
            create_png_bytes,
            owner_id=owner_id,
        ).preview_id

    def get_or_put_entry(
        self,
        cache_key: str,
        create_png_bytes,
        *,
        owner_id: str | None = None,
    ) -> ChromaPreviewEntry:
        if not isinstance(cache_key, str) or not cache_key:
            raise ValueError("cache_key must be a non-empty string")
        cache_identity = (owner_id, cache_key)

        with self._lock:
            existing_entry = self._entry_for_cache_key_locked(cache_identity)
            if existing_entry is not None:
                return existing_entry

            in_flight = self._in_flight_by_cache_key.get(cache_identity)
            if in_flight is None:
                in_flight = _InFlightPreview(completed=Event())
                self._in_flight_by_cache_key[cache_identity] = in_flight
                owns_generation = True
            else:
                owns_generation = False

        if not owns_generation:
            in_flight.completed.wait()
            if in_flight.error is not None:
                raise in_flight.error
            if in_flight.entry is not None:
                return in_flight.entry
            raise RuntimeError("preview generation completed without an entry")

        try:
            png_bytes = create_png_bytes()
            if not isinstance(png_bytes, bytes) or not png_bytes:
                raise ValueError("png_bytes must be non-empty bytes")

            with self._lock:
                existing_entry = self._entry_for_cache_key_locked(cache_identity)
                in_flight.entry = (
                    existing_entry
                    if existing_entry is not None
                    else self._insert_locked(
                        png_bytes,
                        cache_identity=cache_identity,
                        owner_id=owner_id,
                    )
                )
        except BaseException as exc:
            with self._lock:
                if self._in_flight_by_cache_key.get(cache_identity) is in_flight:
                    del self._in_flight_by_cache_key[cache_identity]
                in_flight.error = exc
                in_flight.completed.set()
            raise

        with self._lock:
            if self._in_flight_by_cache_key.get(cache_identity) is in_flight:
                del self._in_flight_by_cache_key[cache_identity]
            in_flight.completed.set()
        if in_flight.entry is None:
            raise RuntimeError("preview generation completed without an entry")
        return in_flight.entry

    def get(
        self,
        preview_id: str,
        *,
        owner_id: str | None = None,
    ) -> bytes | None:
        with self._lock:
            entry = self._previews.get(preview_id)
            if entry is None or entry.owner_id != owner_id:
                return None
            return entry.png_bytes

    def _entry_for_cache_key_locked(
        self,
        cache_identity: tuple[str | None, str],
    ) -> ChromaPreviewEntry | None:
        preview_id = self._preview_ids_by_cache_key.get(cache_identity)
        entry = self._previews.get(preview_id)
        if entry is None or entry.owner_id != cache_identity[0]:
            self._preview_ids_by_cache_key.pop(cache_identity, None)
            return None
        return entry

    def _insert_locked(
        self,
        png_bytes: bytes,
        *,
        cache_identity: tuple[str | None, str] | None,
        owner_id: str | None,
    ) -> ChromaPreviewEntry:
        entry = ChromaPreviewEntry(
            preview_id=str(uuid4()),
            png_bytes=png_bytes,
            owner_id=owner_id,
        )
        self._previews[entry.preview_id] = entry
        if cache_identity is not None:
            self._preview_ids_by_cache_key[cache_identity] = entry.preview_id
        if len(self._previews) > self._max_entries:
            evicted_preview_id, _ = self._previews.popitem(last=False)
            for key, mapped_preview_id in self._preview_ids_by_cache_key.items():
                if mapped_preview_id == evicted_preview_id:
                    del self._preview_ids_by_cache_key[key]
                    break
        return entry
