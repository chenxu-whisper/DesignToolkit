from collections import Counter
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


def test_repeated_attacker_inserts_preserve_each_resident_owner() -> None:
    store = ChromaPreviewStore(max_entries=32)
    victims: dict[str, list[str]] = {}
    for owner_number in range(8):
        owner_id = f"victim-{owner_number}"
        victims[owner_id] = [
            store.put(
                f"{owner_id}-{entry_number}".encode(),
                owner_id=owner_id,
            )
            for entry_number in range(4)
        ]

    attacker_ids = [
        store.put(f"attack-{entry_number}".encode(), owner_id="attacker")
        for entry_number in range(100)
    ]

    for owner_id, preview_ids in victims.items():
        assert any(
            store.get(preview_id, owner_id=owner_id) is not None
            for preview_id in preview_ids
        )
    assert (
        sum(
            store.get(preview_id, owner_id="attacker") is not None
            for preview_id in attacker_ids
        )
        <= 8
    )


def test_global_fairness_tie_evicts_oldest_entry_deterministically() -> None:
    store = ChromaPreviewStore(max_entries=4, max_entries_per_owner=2)
    oldest = store.put(b"a", owner_id="owner-a")
    residents = [
        store.put(owner_id.encode(), owner_id=owner_id)
        for owner_id in ("owner-b", "owner-c", "owner-d")
    ]

    newcomer = store.put(b"e", owner_id="owner-e")

    assert store.get(oldest, owner_id="owner-a") is None
    assert all(
        store.get(preview_id, owner_id=owner_id) is not None
        for preview_id, owner_id in zip(
            residents,
            ("owner-b", "owner-c", "owner-d"),
            strict=True,
        )
    )
    assert store.get(newcomer, owner_id="owner-e") == b"e"


def test_global_fairness_tie_prefers_current_owner_over_resident_victims() -> None:
    store = ChromaPreviewStore(max_entries=4, max_entries_per_owner=2)
    victims = {
        owner_id: store.put(owner_id.encode(), owner_id=owner_id)
        for owner_id in ("owner-a", "owner-b", "owner-c")
    }
    first_attacker = store.put(b"first", owner_id="attacker")

    second_attacker = store.put(b"second", owner_id="attacker")

    assert all(
        store.get(preview_id, owner_id=owner_id) is not None
        for owner_id, preview_id in victims.items()
    )
    assert store.get(first_attacker, owner_id="attacker") is None
    assert store.get(second_attacker, owner_id="attacker") == b"second"


def test_same_cache_key_does_not_consume_owner_budget_twice() -> None:
    store = ChromaPreviewStore(max_entries=4, max_entries_per_owner=2)

    first = store.put(b"first", cache_key="same", owner_id="owner-a")
    duplicate = store.put(b"replacement", cache_key="same", owner_id="owner-a")
    second = store.put(b"second", owner_id="owner-a")

    assert duplicate == first
    assert store.get(first, owner_id="owner-a") == b"first"
    assert store.get(second, owner_id="owner-a") == b"second"
    assert store._owner_counts == {"owner-a": 2}


def test_expired_entries_are_purged_before_capacity_eviction() -> None:
    now = [0.0]
    store = ChromaPreviewStore(
        max_entries=2,
        ttl_seconds=10,
        clock=lambda: now[0],
    )
    expired = store.put(
        b"expired",
        cache_key="expired",
        owner_id="owner-a",
    )
    now[0] = 11.0

    resident_b = store.put(b"b", owner_id="owner-b")
    resident_c = store.put(b"c", owner_id="owner-c")

    assert store.get(expired, owner_id="owner-a") is None
    assert store.get(resident_b, owner_id="owner-b") == b"b"
    assert store.get(resident_c, owner_id="owner-c") == b"c"
    assert store._owner_counts == {"owner-b": 1, "owner-c": 1}
    assert ("owner-a", "expired") not in store._preview_ids_by_cache_key
    assert expired not in store._cache_keys_by_preview_id


def test_delete_and_clear_release_owner_counts_and_cache_keys() -> None:
    store = ChromaPreviewStore(max_entries=4, max_entries_per_owner=2)
    deleted = store.put(b"a", cache_key="a", owner_id="owner-a")
    retained = store.put(b"b", cache_key="b", owner_id="owner-b")

    assert store.delete(deleted, owner_id="owner-a")
    assert not store.delete(retained, owner_id="owner-a")
    assert store._owner_counts == {"owner-b": 1}
    replacement = store.put(b"new-a", cache_key="a", owner_id="owner-a")
    assert replacement != deleted

    store.clear()

    assert store.get(retained, owner_id="owner-b") is None
    assert store.get(replacement, owner_id="owner-a") is None
    assert store._owner_counts == {}
    assert store._preview_ids_by_cache_key == {}
    assert store._cache_keys_by_preview_id == {}
    assert store._expires_at_by_preview_id == {}


def test_concurrent_inserts_preserve_capacity_and_owner_count_invariants() -> None:
    store = ChromaPreviewStore(max_entries=32)

    with ThreadPoolExecutor(max_workers=16) as executor:
        preview_ids = list(
            executor.map(
                lambda item: store.put(
                    f"{item[0]}-{item[1]}".encode(),
                    owner_id=f"owner-{item[0]}",
                ),
                (
                    (owner_number, entry_number)
                    for owner_number in range(8)
                    for entry_number in range(50)
                ),
            )
        )

    resident_entries = list(store._previews.values())
    actual_counts = Counter(
        entry.owner_id for entry in resident_entries if entry.owner_id is not None
    )
    assert len(preview_ids) == 400
    assert len(resident_entries) <= 32
    assert all(count <= 8 for count in actual_counts.values())
    assert store._owner_counts == dict(actual_counts)
    assert sum(store._owner_counts.values()) == len(resident_entries)


@pytest.mark.parametrize("max_entries", [1, 2])
def test_small_capacities_remain_bounded(max_entries: int) -> None:
    store = ChromaPreviewStore(max_entries=max_entries)
    victim_a = store.put(b"a", owner_id="owner-a")
    victim_b = (
        store.put(b"b", owner_id="owner-b") if max_entries == 2 else None
    )

    attacker_ids = [
        store.put(f"attack-{index}".encode(), owner_id="attacker")
        for index in range(20)
    ]

    assert len(store._previews) == max_entries
    assert store._owner_counts["attacker"] == 1
    assert (
        sum(
            store.get(preview_id, owner_id="attacker") is not None
            for preview_id in attacker_ids
        )
        == 1
    )
    assert store.get(victim_a, owner_id="owner-a") is None
    if victim_b is not None:
        assert store.get(victim_b, owner_id="owner-b") == b"b"


def test_local_capacity_keeps_global_fifo_semantics() -> None:
    store = ChromaPreviewStore(max_entries=2)
    oldest = store.put(b"oldest")
    middle = store.put(b"middle")

    newest = store.put(b"newest")

    assert store.get(oldest) is None
    assert store.get(middle) == b"middle"
    assert store.get(newest) == b"newest"
    assert store._owner_counts == {}
