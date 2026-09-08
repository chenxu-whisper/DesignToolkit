from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier, Event, Lock

import pytest

from designtoolkit.web.chroma_previews import ChromaPreviewStore


def test_fifo_eviction_keeps_resident_owners_when_first_response_is_delayed() -> None:
    first_inserted = Event()
    release_first_response = Event()

    class DelayedResponseStore(ChromaPreviewStore):
        def get_or_put_entry(
            self,
            cache_key: str,
            create_png_bytes,
            *,
            owner_id: str | None = None,
        ):
            entry = super().get_or_put_entry(
                cache_key,
                create_png_bytes,
                owner_id=owner_id,
            )
            if cache_key == "evicted":
                first_inserted.set()
                assert release_first_response.wait(timeout=5)
            return entry

    store = DelayedResponseStore(max_entries=2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        delayed = executor.submit(
            store.get_or_put_entry,
            "evicted",
            lambda: b"first",
            owner_id="owner-a",
        )
        assert first_inserted.wait(timeout=5)
        resident_a = store.get_or_put_entry(
            "resident-a",
            lambda: b"second",
            owner_id="owner-a",
        )
        resident_b = store.get_or_put_entry(
            "resident-b",
            lambda: b"third",
            owner_id="owner-b",
        )
        release_first_response.set()
        evicted = delayed.result(timeout=5)

    assert store.get(evicted.preview_id, owner_id="owner-a") is None
    assert store.get(resident_a.preview_id, owner_id="owner-a") == b"second"
    assert store.get(resident_b.preview_id, owner_id="owner-b") == b"third"
    assert store.get(resident_a.preview_id, owner_id="owner-b") is None
    assert store.get(resident_b.preview_id, owner_id="owner-a") is None
    replacement = store.get_or_put_entry(
        "evicted",
        lambda: b"replacement",
        owner_id="owner-a",
    )
    assert replacement.preview_id != evicted.preview_id


def test_same_cache_key_is_scoped_to_owner() -> None:
    store = ChromaPreviewStore(max_entries=2)

    owner_a = store.get_or_put_entry(
        "shared-key",
        lambda: b"owner-a",
        owner_id="owner-a",
    )
    owner_b = store.get_or_put_entry(
        "shared-key",
        lambda: b"owner-b",
        owner_id="owner-b",
    )

    assert owner_a.preview_id != owner_b.preview_id
    assert store.get(owner_a.preview_id, owner_id="owner-a") == b"owner-a"
    assert store.get(owner_b.preview_id, owner_id="owner-b") == b"owner-b"
    assert store.get(owner_a.preview_id, owner_id="owner-b") is None
    assert store.get(owner_b.preview_id, owner_id="owner-a") is None


def test_same_owner_and_cache_key_share_singleflight_generation() -> None:
    store = ChromaPreviewStore(max_entries=2)
    start = Barrier(3)
    generation_started = Event()
    release_generation = Event()
    generation_calls = 0
    generation_calls_lock = Lock()

    def create_png() -> bytes:
        nonlocal generation_calls
        with generation_calls_lock:
            generation_calls += 1
        generation_started.set()
        assert release_generation.wait(timeout=5)
        return b"preview"

    def get_preview():
        start.wait(timeout=5)
        return store.get_or_put_entry(
            "shared-key",
            create_png,
            owner_id="owner-a",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(get_preview)
        second = executor.submit(get_preview)
        start.wait(timeout=5)
        assert generation_started.wait(timeout=5)
        with pytest.raises(TimeoutError):
            second.result(timeout=0.05)
        release_generation.set()
        first_entry = first.result(timeout=5)
        second_entry = second.result(timeout=5)

    assert generation_calls == 1
    assert first_entry.preview_id == second_entry.preview_id
    assert store.get(first_entry.preview_id, owner_id="owner-a") == b"preview"


def test_local_preview_operations_remain_ownerless() -> None:
    store = ChromaPreviewStore(max_entries=2)

    preview_id = store.put(b"local", cache_key="shared-key")
    deduplicated_id = store.get_or_put(
        "shared-key",
        lambda: b"replacement",
    )

    assert deduplicated_id == preview_id
    assert store.get(preview_id) == b"local"
