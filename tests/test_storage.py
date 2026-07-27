"""Tests for the persistent seen-store."""

from typing import Any

from homeassistant.core import HomeAssistant
import pytest

from custom_components.rss_notify.const import MAX_SEEN_KEYS, STORAGE_VERSION
from custom_components.rss_notify.storage import SeenStore, storage_key

ENTRY_ID = "01JABCDEF0123456789"
STORE_KEY = f"rss_notify.{ENTRY_ID}"


def seed(
    hass_storage: dict[str, Any],
    seen: list[str],
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    """Pre-populate the mocked storage with state for the test entry."""
    hass_storage[STORE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {"seen": seen, "etag": etag, "last_modified": last_modified},
    }


def test_storage_key() -> None:
    """The storage key is namespaced per config entry."""
    assert storage_key(ENTRY_ID) == STORE_KEY


async def test_load_empty(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Loading a feed with no persisted state yields empty state."""
    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()

    assert store.is_new is True
    assert store.seen_count == 0
    assert store.etag is None
    assert store.last_modified is None
    assert store.contains("post-1") is False


async def test_files_are_written_and_read_as_schema_version_one(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The on-disk schema is version 1, spelled out rather than imported.

    Bumping `STORAGE_VERSION` without adding a migration would make `Store`
    raise `NotImplementedError` on every file written by an earlier release, so
    the version is pinned here: a bump has to fail this test on purpose.
    """
    assert STORAGE_VERSION == 1
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {"seen": ["post-1"], "etag": None, "last_modified": None},
    }

    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()
    store.add(["post-2"])
    await store.async_save()

    assert store.contains("post-1") is True
    assert hass_storage[STORE_KEY]["version"] == 1


async def test_load_existing(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Persisted keys and validators are loaded back."""
    seed(
        hass_storage,
        ["post-1", "post-2"],
        etag='"abc"',
        last_modified="Fri, 24 Jul 2026 12:00:00 GMT",
    )

    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()

    assert store.is_new is False
    assert store.seen_count == 2
    assert store.contains("post-1") is True
    assert store.contains("post-3") is False
    assert store.etag == '"abc"'
    assert store.last_modified == "Fri, 24 Jul 2026 12:00:00 GMT"


async def test_load_tolerates_partial_data(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A file written by an older/partial writer still loads."""
    hass_storage[STORE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {},
    }

    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()

    assert store.is_new is False
    assert store.seen_count == 0
    assert store.etag is None


async def test_add_save_reload_roundtrip(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Added keys and validators survive a save and a fresh store instance."""
    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()

    store.add(["post-1", "post-2"])
    store.add(["post-2", "post-3"])  # duplicates are ignored
    store.etag = '"xyz"'
    store.last_modified = "Sat, 25 Jul 2026 09:00:00 GMT"
    await store.async_save()

    assert store.is_new is False
    assert hass_storage[STORE_KEY]["version"] == STORAGE_VERSION
    assert hass_storage[STORE_KEY]["data"] == {
        "seen": ["post-1", "post-2", "post-3"],
        "etag": '"xyz"',
        "last_modified": "Sat, 25 Jul 2026 09:00:00 GMT",
    }

    reloaded = SeenStore(hass, ENTRY_ID)
    await reloaded.async_load()

    assert reloaded.is_new is False
    assert reloaded.seen_count == 3
    assert all(reloaded.contains(key) for key in ("post-1", "post-2", "post-3"))
    assert reloaded.etag == '"xyz"'
    assert reloaded.last_modified == "Sat, 25 Jul 2026 09:00:00 GMT"


async def test_cleared_validators_are_persisted(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Setting validators back to None clears them on disk too."""
    seed(hass_storage, ["post-1"], etag='"abc"', last_modified="stale")

    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()
    store.etag = None
    store.last_modified = None
    await store.async_save()

    assert hass_storage[STORE_KEY]["data"]["etag"] is None
    assert hass_storage[STORE_KEY]["data"]["last_modified"] is None


async def test_prune_keeps_newest_keys_on_save(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Saving prunes the oldest keys above the cap, in memory and on disk."""
    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()

    overflow = 10
    store.add(f"post-{index}" for index in range(MAX_SEEN_KEYS + overflow))
    await store.async_save()

    assert store.seen_count == MAX_SEEN_KEYS
    assert store.contains("post-0") is False
    assert store.contains(f"post-{overflow - 1}") is False
    assert store.contains(f"post-{overflow}") is True
    assert store.contains(f"post-{MAX_SEEN_KEYS + overflow - 1}") is True

    persisted = hass_storage[STORE_KEY]["data"]["seen"]
    assert len(persisted) == MAX_SEEN_KEYS
    assert persisted[0] == f"post-{overflow}"
    assert persisted[-1] == f"post-{MAX_SEEN_KEYS + overflow - 1}"


async def test_touch_moves_known_keys_to_the_newest_end(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Touching a key makes it survive pruning longer; unknown keys are ignored."""
    seed(hass_storage, ["post-1", "post-2", "post-3"])

    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()
    store.touch(["post-1", "never-seen"])
    await store.async_save()

    assert store.contains("never-seen") is False
    assert hass_storage[STORE_KEY]["data"]["seen"] == ["post-2", "post-3", "post-1"]


async def test_touching_all_keys_keeps_the_seen_set_at_its_size(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Touching every key reorders nothing away: a touch never drops a key."""
    seed(hass_storage, ["post-1", "post-2"])

    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()
    store.touch(["post-2", "post-1"])

    assert store.seen_count == 2
    assert store.contains("post-1") is True
    assert store.contains("post-2") is True


async def test_prune_never_drops_a_key_the_feed_still_lists(
    hass: HomeAssistant, hass_storage: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document larger than the cap keeps all of its keys, so nothing repeats.

    Pruning to the cap regardless would drop keys of items the document still
    lists, and the next poll would announce those items again - forever.
    """
    monkeypatch.setattr("custom_components.rss_notify.storage.MAX_SEEN_KEYS", 3)
    seed(hass_storage, ["gone-1", "gone-2"])

    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()
    listed = ["post-1", "post-2", "post-3", "post-4"]
    store.touch(listed)
    store.add(listed)
    await store.async_save()

    # the cap is 3, yet all four listed keys survive; only dropped ones go
    assert hass_storage[STORE_KEY]["data"]["seen"] == listed
    assert all(store.contains(key) for key in listed)


async def test_save_at_cap_does_not_prune(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A seen-set exactly at the cap is persisted untouched."""
    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()

    store.add(f"post-{index}" for index in range(MAX_SEEN_KEYS))
    await store.async_save()

    assert store.seen_count == MAX_SEEN_KEYS
    assert store.contains("post-0") is True
    assert len(hass_storage[STORE_KEY]["data"]["seen"]) == MAX_SEEN_KEYS


async def test_remove_deletes_file_and_state(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Removing the store deletes the file and resets in-memory state."""
    seed(hass_storage, ["post-1"], etag='"abc"')

    store = SeenStore(hass, ENTRY_ID)
    await store.async_load()
    await store.async_remove()

    assert STORE_KEY not in hass_storage
    assert store.seen_count == 0
    assert store.etag is None
    assert store.last_modified is None
    assert store.is_new is True

    reloaded = SeenStore(hass, ENTRY_ID)
    await reloaded.async_load()

    assert reloaded.is_new is True
    assert reloaded.seen_count == 0


async def test_stores_are_isolated_per_entry(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Two feeds keep independent seen-sets."""
    first = SeenStore(hass, "entry-a")
    second = SeenStore(hass, "entry-b")
    await first.async_load()
    await second.async_load()

    first.add(["shared-key"])
    await first.async_save()

    assert second.contains("shared-key") is False
    assert "rss_notify.entry-a" in hass_storage
    assert "rss_notify.entry-b" not in hass_storage
