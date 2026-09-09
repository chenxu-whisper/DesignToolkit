from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

_DEFAULT_TTL_SECONDS = 300.0


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
    """Thread-safe TTL store with bounded, owner-aware FIFO eviction.

    Hosted owners have individual budgets and fair global eviction. Ownerless
    local previews retain the original global FIFO capacity behavior.
    """

    def __init__(
        self,
        max_entries: int = 32,
        *,
        max_entries_per_owner: int | None = None,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or max_entries <= 0
        ):
            raise ValueError("max_entries must be a positive integer")
        max_owner_budget = max_entries if max_entries == 1 else max_entries - 1
        if max_entries_per_owner is None:
            max_entries_per_owner = max(1, min(8, max_entries // 4))
        if (
            not isinstance(max_entries_per_owner, int)
            or isinstance(max_entries_per_owner, bool)
            or max_entries_per_owner <= 0
            or max_entries_per_owner > max_owner_budget
        ):
            raise ValueError(
                "max_entries_per_owner must be a positive integer below "
                "max_entries"
            )
        if (
            not isinstance(ttl_seconds, int | float)
            or isinstance(ttl_seconds, bool)
            or not isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive finite number")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._max_entries = max_entries
        self._max_entries_per_owner = max_entries_per_owner
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._previews: OrderedDict[str, ChromaPreviewEntry] = OrderedDict()
        self._preview_ids_by_cache_key: dict[tuple[str | None, str], str] = {}
        self._cache_keys_by_preview_id: dict[
            str,
            tuple[str | None, str],
        ] = {}
        self._expires_at_by_preview_id: dict[str, float] = {}
        self._owner_counts: dict[str, int] = {}
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
            self._purge_expired_locked()
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
            self._purge_expired_locked()
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
                self._purge_expired_locked()
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
            self._purge_expired_locked()
            entry = self._previews.get(preview_id)
            if entry is None or entry.owner_id != owner_id:
                return None
            return entry.png_bytes

    def delete(
        self,
        preview_id: str,
        *,
        owner_id: str | None = None,
    ) -> bool:
        with self._lock:
            self._purge_expired_locked()
            entry = self._previews.get(preview_id)
            if entry is None or entry.owner_id != owner_id:
                return False
            self._remove_locked(preview_id)
            return True

    def clear(self) -> None:
        with self._lock:
            self._previews.clear()
            self._preview_ids_by_cache_key.clear()
            self._cache_keys_by_preview_id.clear()
            self._expires_at_by_preview_id.clear()
            self._owner_counts.clear()

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
        self._purge_expired_locked()
        if owner_id is None:
            if len(self._previews) >= self._max_entries:
                self._remove_locked(next(iter(self._previews)))
        elif self._owner_counts.get(owner_id, 0) >= self._max_entries_per_owner:
            self._remove_locked(self._oldest_preview_id_for_owner_locked(owner_id))
        elif len(self._previews) >= self._max_entries:
            self._remove_locked(self._fair_eviction_preview_id_locked(owner_id))

        entry = ChromaPreviewEntry(
            preview_id=str(uuid4()),
            png_bytes=png_bytes,
            owner_id=owner_id,
        )
        self._previews[entry.preview_id] = entry
        self._expires_at_by_preview_id[entry.preview_id] = (
            self._clock() + self._ttl_seconds
        )
        if owner_id is not None:
            self._owner_counts[owner_id] = self._owner_counts.get(owner_id, 0) + 1
        if cache_identity is not None:
            self._preview_ids_by_cache_key[cache_identity] = entry.preview_id
            self._cache_keys_by_preview_id[entry.preview_id] = cache_identity
        return entry

    def _purge_expired_locked(self) -> None:
        now = self._clock()
        expired_preview_ids = [
            preview_id
            for preview_id, expires_at in self._expires_at_by_preview_id.items()
            if expires_at <= now
        ]
        for preview_id in expired_preview_ids:
            self._remove_locked(preview_id)

    def _remove_locked(self, preview_id: str) -> ChromaPreviewEntry | None:
        entry = self._previews.pop(preview_id, None)
        if entry is None:
            return None

        self._expires_at_by_preview_id.pop(preview_id, None)
        cache_identity = self._cache_keys_by_preview_id.pop(preview_id, None)
        if (
            cache_identity is not None
            and self._preview_ids_by_cache_key.get(cache_identity) == preview_id
        ):
            del self._preview_ids_by_cache_key[cache_identity]
        if entry.owner_id is not None:
            remaining = self._owner_counts[entry.owner_id] - 1
            if remaining:
                self._owner_counts[entry.owner_id] = remaining
            else:
                del self._owner_counts[entry.owner_id]
        return entry

    def _oldest_preview_id_for_owner_locked(self, owner_id: str) -> str:
        for preview_id, entry in self._previews.items():
            if entry.owner_id == owner_id:
                return preview_id
        raise RuntimeError("preview owner count is inconsistent")

    def _fair_eviction_preview_id_locked(self, current_owner_id: str) -> str:
        occupancies: dict[str | None, int] = dict(self._owner_counts)
        local_count = len(self._previews) - sum(self._owner_counts.values())
        if local_count:
            occupancies[None] = local_count
        largest_occupancy = max(occupancies.values())
        if occupancies.get(current_owner_id) == largest_occupancy:
            return self._oldest_preview_id_for_owner_locked(current_owner_id)
        for preview_id, entry in self._previews.items():
            if occupancies[entry.owner_id] == largest_occupancy:
                return preview_id
        raise RuntimeError("preview occupancy is inconsistent")
