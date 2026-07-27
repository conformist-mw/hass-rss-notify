"""Tests for the feed coordinator: dedup, ordering, initial sync and trickle."""

from http import HTTPStatus
import logging
from typing import Any

import aiohttp
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.const import (
    CONF_INITIAL_ITEMS,
    CONF_MAX_ITEMS_PER_POLL,
    EVENT_NEW_ITEM,
)
from custom_components.rss_notify.coordinator import RssFeedCoordinator
from custom_components.rss_notify.storage import SeenStore, storage_key

from .conftest import (
    FEED_LINK,
    FEED_TITLE,
    LAST_MODIFIED,
    feed_bytes,
    load_feed,
    make_config_entry,
    seed_store,
    sent_headers,
    serve,
    serve_keys,
    stored,
)

TWENTY_FIVE = [f"post-{index}" for index in range(1, 26)]


def emitted(coordinator: RssFeedCoordinator) -> list[str]:
    """Return the keys emitted by the last poll of `coordinator`."""
    return [item.key for item in coordinator.data.new_items]


async def build_coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> RssFeedCoordinator:
    """Build a coordinator over a freshly loaded store, as entry setup does."""
    store = SeenStore(hass, entry.entry_id)
    await store.async_load()
    return RssFeedCoordinator(hass, entry, store)


@pytest.mark.parametrize(
    ("initial_items", "expected"),
    [
        (1, ["post-3"]),
        (0, []),
        (2, ["post-2", "post-3"]),
        (5, ["post-1", "post-2", "post-3"]),
    ],
)
async def test_initial_sync(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    initial_items: int,
    expected: list[str],
) -> None:
    """The first refresh emits the newest `initial_items` and silences the rest."""
    entry = make_config_entry(**{CONF_INITIAL_ITEMS: initial_items})
    entry.add_to_hass(hass)
    serve_keys(aioclient_mock, ["post-1", "post-2", "post-3"], etag='"v1"')

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert emitted(coordinator) == expected
    assert coordinator.data.pending == 0
    assert coordinator.feed_title == FEED_TITLE
    assert coordinator.feed_link == FEED_LINK

    # every item of the feed is marked seen, emitted or not
    persisted = stored(hass_storage, entry.entry_id)
    assert persisted["seen"] == ["post-1", "post-2", "post-3"]
    assert persisted["etag"] == '"v1"'
    assert coordinator.store.seen_count == 3


async def test_initial_sync_is_not_capped_by_max_items_per_poll(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """`initial_items` always wins over `max_items_per_poll` on the first poll.

    The cap protects against a publisher dumping a burst of new items; the
    initial sync announces a number the user asked for explicitly, once.
    """
    entry = make_config_entry(**{CONF_INITIAL_ITEMS: 8, CONF_MAX_ITEMS_PER_POLL: 2})
    entry.add_to_hass(hass)
    serve_keys(aioclient_mock, TWENTY_FIVE)

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == TWENTY_FIVE[-8:]
    assert coordinator.data.pending == 0
    # every item is seen, so the cap only governs the polls that follow
    assert stored(hass_storage, entry.entry_id)["seen"] == TWENTY_FIVE


async def test_a_feed_that_is_empty_when_added_syncs_on_its_first_items(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """An empty feed keeps its initial sync until it publishes something.

    Persisting an empty seen-set would end the feed's "new" state, so the batch
    it publishes later would arrive as a capped steady-state poll instead of the
    promised `initial_items` announcement.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    serve(aioclient_mock, content=feed_bytes([]))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == []
    assert storage_key(entry.entry_id) not in hass_storage

    # the feed fills up: exactly one item is announced, the rest stays silent
    serve_keys(aioclient_mock, TWENTY_FIVE)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["post-25"]
    assert coordinator.data.pending == 0
    assert stored(hass_storage, entry.entry_id)["seen"] == TWENTY_FIVE


async def test_new_items_across_polls(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """After the initial sync, later polls emit only genuinely new items."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    coordinator = await build_coordinator(hass, entry)

    serve_keys(aioclient_mock, ["post-1", "post-2", "post-3"])
    await coordinator.async_refresh()
    assert emitted(coordinator) == ["post-3"]

    serve_keys(aioclient_mock, ["post-1", "post-2", "post-3", "post-4", "post-5"])
    await coordinator.async_refresh()
    assert emitted(coordinator) == ["post-4", "post-5"]
    assert coordinator.data.pending == 0

    # unchanged feed content emits nothing, even though the feed is re-fetched
    serve_keys(aioclient_mock, ["post-1", "post-2", "post-3", "post-4", "post-5"])
    await coordinator.async_refresh()
    assert emitted(coordinator) == []
    assert stored(hass_storage, entry.entry_id)["seen"] == [
        "post-1",
        "post-2",
        "post-3",
        "post-4",
        "post-5",
    ]


async def test_new_items_emitted_oldest_first_undated_first(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """New items are emitted oldest to newest, undated items first.

    The fixture lists undated-a, dated-b, undated-c, dated-d. Documents list the
    newest item first, so the undated pair is emitted bottom-up.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    # a feed that was synced before, so this poll takes the steady-state path
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve(aioclient_mock, content=load_feed("feed_no_dates"))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["undated-c", "undated-a", "dated-d", "dated-b"]
    assert stored(hass_storage, entry.entry_id)["seen"] == [
        "already-seen",
        "undated-c",
        "undated-a",
        "dated-d",
        "dated-b",
    ]


async def test_undated_feed_announces_the_topmost_item_first_time(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """On a feed that dates nothing, the initial item is the topmost one."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    serve(aioclient_mock, content=load_feed("feed_undated"))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["u-3"]


async def test_undated_feed_trickles_out_bottom_up(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A feed that dates nothing is announced from the bottom of the document up."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve(aioclient_mock, content=load_feed("feed_undated"))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["u-1", "u-2", "u-3"]


async def test_repeated_key_in_one_poll_is_emitted_once(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """An item listed twice in the same document is emitted once."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    # the feed lists post-2 twice, with different dates
    serve_keys(aioclient_mock, ["post-1", "post-2", "post-2"])

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["post-1", "post-2"]
    assert coordinator.data.pending == 0
    assert stored(hass_storage, entry.entry_id)["seen"] == [
        "already-seen",
        "post-1",
        "post-2",
    ]


async def test_repeated_key_in_the_initial_sync_is_emitted_once(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The initial batch counts a repeated item once, not once per listing."""
    entry = make_config_entry(**{CONF_INITIAL_ITEMS: 2})
    entry.add_to_hass(hass)
    # the newest item is listed three times, so without dedup the two newest
    # listings would both be part of the initial batch
    serve_keys(aioclient_mock, ["post-1", "post-2", "post-2", "post-2"])

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["post-1", "post-2"]
    assert stored(hass_storage, entry.entry_id)["seen"] == ["post-1", "post-2"]


async def test_feed_without_guids_dedups_by_link(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A feed whose items carry no guid is deduplicated by item link."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    serve(aioclient_mock, content=load_feed("feed_no_guid"))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["https://noguid.example.com/posts/2"]
    assert stored(hass_storage, entry.entry_id)["seen"] == [
        "https://noguid.example.com/posts/1",
        "https://noguid.example.com/posts/2",
    ]

    # the unchanged feed emits nothing on the next poll: links identify the items
    serve(aioclient_mock, content=load_feed("feed_no_guid"))
    await coordinator.async_refresh()

    assert emitted(coordinator) == []


async def test_items_without_identity_are_ignored_every_poll(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An item with no usable identity is skipped, the fingerprinted one is stable."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve(aioclient_mock, content=load_feed("feed_no_ids"))

    coordinator = await build_coordinator(hass, entry)
    with caplog.at_level(logging.WARNING):
        await coordinator.async_refresh()

    # only the fingerprinted item exists as far as the coordinator is concerned
    assert len(emitted(coordinator)) == 1
    fingerprint = emitted(coordinator)[0]
    assert stored(hass_storage, entry.entry_id)["seen"] == ["already-seen", fingerprint]
    assert "without any usable identity" in caplog.text

    # the fingerprint is stable, so the next poll of the same feed emits nothing
    serve(aioclient_mock, content=load_feed("feed_no_ids"))
    await coordinator.async_refresh()

    assert emitted(coordinator) == []
    assert coordinator.store.seen_count == 2


async def test_not_modified_while_a_backlog_is_pending_keeps_the_backlog(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A 304 arriving mid-trickle changes nothing; the backlog resumes after it.

    The held-back validators make a 304 unlikely, but a server answering one
    anyway must not consume the pending items.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"], etag='"v0"')
    serve_keys(aioclient_mock, TWENTY_FIVE, etag='"v1"')

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()
    assert emitted(coordinator) == TWENTY_FIVE[:10]
    before = dict(stored(hass_storage, entry.entry_id))

    serve(aioclient_mock, status=HTTPStatus.NOT_MODIFIED)
    await coordinator.async_refresh()

    assert emitted(coordinator) == []
    # the backlog is still queued, so it is still reported (diagnostics read it)
    assert coordinator.data.pending == 15
    assert stored(hass_storage, entry.entry_id) == before

    # the next full response continues the trickle where it stopped
    serve_keys(aioclient_mock, TWENTY_FIVE, etag='"v1"')
    await coordinator.async_refresh()

    assert emitted(coordinator) == TWENTY_FIVE[10:20]
    assert coordinator.data.pending == 5


async def test_items_are_published_before_they_are_marked_seen(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The store is saved only after the batch is published (at-least-once).

    Persisting first would turn a crash mid-poll into a lost item instead of a
    repeated one, which is the opposite of the documented guarantee.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve_keys(aioclient_mock, ["post-1", "post-2"])
    seen_at_publish: list[list[str]] = []

    @callback
    def _snapshot(event: Event) -> None:
        seen_at_publish.append(list(stored(hass_storage, entry.entry_id)["seen"]))

    hass.bus.async_listen(EVENT_NEW_ITEM, _snapshot)

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert emitted(coordinator) == ["post-1", "post-2"]
    # neither item was seen on disk while it was being published
    assert seen_at_publish == [["already-seen"], ["already-seen"]]
    assert stored(hass_storage, entry.entry_id)["seen"] == [
        "already-seen",
        "post-1",
        "post-2",
    ]


async def test_store_is_saved_once_per_poll_and_only_when_needed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch costs one save; a poll that changes nothing costs none."""
    saves: list[int] = []
    original = SeenStore.async_save

    async def counting_save(store: SeenStore) -> None:
        saves.append(1)
        await original(store)

    monkeypatch.setattr(SeenStore, "async_save", counting_save)

    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"], etag='"v1"')
    serve_keys(aioclient_mock, TWENTY_FIVE[:10], etag='"v1"')

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    # ten items, one save
    assert len(emitted(coordinator)) == 10
    assert len(saves) == 1

    # the same feed content with the same validators changes nothing to persist
    serve_keys(aioclient_mock, TWENTY_FIVE[:10], etag='"v1"')
    await coordinator.async_refresh()

    assert emitted(coordinator) == []
    assert len(saves) == 1


async def test_the_entry_title_is_the_reported_feed_title(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The feed is named after its entry, never after the document it serves.

    The config flow puts the user's name - or the feed's own title, or its host -
    into the entry title, and the device, the entity and the payload all read it
    from there. Adopting the document's <title> per poll could only make the four
    disagree after a publisher rename.
    """
    entry = make_config_entry(title="Work RSS")
    entry.add_to_hass(hass)
    serve(aioclient_mock, content=feed_bytes(["post-1"], title="Publisher Rename"))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert coordinator.feed_title == "Work RSS"


async def test_feed_link_falls_back_to_the_last_one_reported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A feed that stops reporting a <link> keeps the link it reported before."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    serve(aioclient_mock, content=feed_bytes(["post-1"]))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()
    assert coordinator.feed_link == FEED_LINK

    linkless = feed_bytes(["post-1", "post-2"]).replace(
        f"<link>{FEED_LINK}</link>".encode(), b""
    )
    serve(aioclient_mock, content=linkless)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["post-2"]
    assert coordinator.feed_link == FEED_LINK


async def test_restart_does_not_reemit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A fresh coordinator over the persisted store re-emits nothing."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    serve_keys(aioclient_mock, ["post-1", "post-2", "post-3"])

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()
    assert emitted(coordinator) == ["post-3"]

    # restart: new coordinator, new store instance, same persisted state
    serve_keys(aioclient_mock, ["post-1", "post-2", "post-3"])
    restarted = await build_coordinator(hass, entry)
    await restarted.async_refresh()

    assert emitted(restarted) == []
    assert restarted.data.pending == 0
    assert stored(hass_storage, entry.entry_id)["seen"] == [
        "post-1",
        "post-2",
        "post-3",
    ]


async def test_not_modified_is_a_no_op(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A 304 emits nothing, keeps the feed meta and touches no state."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    serve_keys(aioclient_mock, ["post-1", "post-2"], etag='"v1"')

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()
    before = dict(stored(hass_storage, entry.entry_id))

    serve(aioclient_mock, status=HTTPStatus.NOT_MODIFIED)
    await coordinator.async_refresh()

    assert sent_headers(aioclient_mock)["If-None-Match"] == '"v1"'
    assert coordinator.last_update_success is True
    assert emitted(coordinator) == []
    assert coordinator.data.pending == 0
    # the feed meta of the last full response is kept
    assert coordinator.feed_title == FEED_TITLE
    assert coordinator.feed_link == FEED_LINK
    assert stored(hass_storage, entry.entry_id) == before


async def test_last_modified_is_persisted_and_sent_back(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A feed that only sends `Last-Modified` gets a conditional GET too."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    serve_keys(aioclient_mock, ["post-1", "post-2"], last_modified=LAST_MODIFIED)

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    persisted = stored(hass_storage, entry.entry_id)
    assert persisted["last_modified"] == LAST_MODIFIED
    assert persisted["etag"] is None
    assert coordinator.store.last_modified == LAST_MODIFIED
    assert coordinator.store.etag is None

    serve(aioclient_mock, status=HTTPStatus.NOT_MODIFIED)
    await coordinator.async_refresh()

    headers = sent_headers(aioclient_mock)
    assert headers["If-Modified-Since"] == LAST_MODIFIED
    assert "If-None-Match" not in headers
    assert coordinator.last_update_success is True
    assert emitted(coordinator) == []


async def test_last_modified_is_cleared_while_a_backlog_is_pending(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A pending backlog clears the stored `Last-Modified`, like it clears the ETag."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(
        hass_storage,
        entry.entry_id,
        ["already-seen"],
        last_modified="Tue, 21 Jul 2026 12:00:00 GMT",
    )
    serve_keys(aioclient_mock, TWENTY_FIVE, last_modified=LAST_MODIFIED)

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert coordinator.data.pending == 15
    assert stored(hass_storage, entry.entry_id)["last_modified"] is None

    # so the next poll is unconditional and cannot be answered with a 304
    serve_keys(aioclient_mock, TWENTY_FIVE, last_modified=LAST_MODIFIED)
    await coordinator.async_refresh()

    assert "If-Modified-Since" not in sent_headers(aioclient_mock)


async def test_items_still_listed_do_not_age_out_of_the_seen_set(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruning never drops the key of an item the feed still lists.

    With the seen-set at its cap, adding a new key prunes the oldest one. If
    that key belonged to an item still in the document - a pinned post, a feed
    serving its whole archive - the next poll would announce it a second time.
    """
    monkeypatch.setattr("custom_components.rss_notify.storage.MAX_SEEN_KEYS", 3)
    entry = make_config_entry()
    entry.add_to_hass(hass)
    # the seen-set is full and its oldest key is an item the feed still lists
    seed_store(hass_storage, entry.entry_id, ["post-1", "gone-1", "gone-2"])
    serve_keys(aioclient_mock, ["post-1", "post-2"])

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["post-2"]
    # the key that aged out belongs to an item the feed dropped long ago
    assert stored(hass_storage, entry.entry_id)["seen"] == [
        "gone-2",
        "post-1",
        "post-2",
    ]

    serve_keys(aioclient_mock, ["post-1", "post-2"])
    await coordinator.async_refresh()

    assert emitted(coordinator) == []


async def test_unlimited_cap_emits_everything(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """`max_items_per_poll` of 0 means no cap, so nothing is held back."""
    entry = make_config_entry(**{CONF_MAX_ITEMS_PER_POLL: 0})
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve_keys(aioclient_mock, TWENTY_FIVE, etag='"v1"')

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == TWENTY_FIVE
    assert coordinator.data.pending == 0
    assert stored(hass_storage, entry.entry_id)["etag"] == '"v1"'


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"exc": aiohttp.ClientError("boom")}, "Error fetching feed"),
        ({"status": HTTPStatus.INTERNAL_SERVER_ERROR}, "Error fetching feed"),
        ({"content": load_feed("feed_malformed")}, "Unable to parse feed"),
    ],
)
async def test_fetch_and_parse_failures_raise_update_failed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    kwargs: dict[str, Any],
    match: str,
) -> None:
    """Fetch and parse problems fail the update and leave the seen-set alone."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"], etag='"v1"')
    serve(aioclient_mock, **kwargs)

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert match in str(coordinator.last_exception)
    assert coordinator.data is None
    assert stored(hass_storage, entry.entry_id) == {
        "seen": ["already-seen"],
        "etag": '"v1"',
        "last_modified": None,
    }


async def test_backlog_clears_the_stored_validators(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """With a backlog pending the stored validators are cleared, not updated.

    Keeping the previous ones would only make a 304 unlikely: a server whose
    current validator equals the stored one - a coarse `Last-Modified`, or a 200
    served with an unchanged ETag - would strand the backlog. No validator at
    all guarantees the full response the trickle needs.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"], etag='"v0"')
    serve_keys(aioclient_mock, TWENTY_FIVE, etag='"v1"')

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert sent_headers(aioclient_mock)["If-None-Match"] == '"v0"'
    assert coordinator.data.pending == 15
    assert stored(hass_storage, entry.entry_id)["etag"] is None

    serve_keys(aioclient_mock, TWENTY_FIVE, etag='"v1"')
    await coordinator.async_refresh()

    assert "If-None-Match" not in sent_headers(aioclient_mock)


async def test_trickle_holds_validators_across_restart(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A capped backlog trickles out, survives a restart, then allows a 304.

    25 new items with a fresh ETag: 10 are emitted and the new validators are
    held back, so the next poll re-fetches a full 200 and continues. The third
    batch is emitted by a *fresh* coordinator over the persisted store (an HA
    restart mid-trickle); only then are the validators persisted, which lets the
    following poll settle into a cheap 304.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    # the feed was synced earlier, so all 25 items count as new
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    coordinator = await build_coordinator(hass, entry)

    serve_keys(aioclient_mock, TWENTY_FIVE, etag='"v1"')
    await coordinator.async_refresh()

    assert emitted(coordinator) == TWENTY_FIVE[:10]
    assert coordinator.data.pending == 15
    persisted = stored(hass_storage, entry.entry_id)
    assert persisted["seen"] == ["already-seen", *TWENTY_FIVE[:10]]
    assert persisted["etag"] is None, "validators must be held back"

    # poll 2: no validators are sent, so the server answers with a full 200
    serve_keys(aioclient_mock, TWENTY_FIVE, etag='"v1"')
    await coordinator.async_refresh()

    assert "If-None-Match" not in sent_headers(aioclient_mock)
    assert emitted(coordinator) == TWENTY_FIVE[10:20]
    assert coordinator.data.pending == 5
    assert stored(hass_storage, entry.entry_id)["etag"] is None

    # HA restarts mid-trickle: a fresh coordinator reads the persisted store
    serve_keys(aioclient_mock, TWENTY_FIVE, etag='"v1"')
    restarted = await build_coordinator(hass, entry)
    await restarted.async_refresh()

    assert "If-None-Match" not in sent_headers(aioclient_mock)
    assert emitted(restarted) == TWENTY_FIVE[20:]
    assert restarted.data.pending == 0
    persisted = stored(hass_storage, entry.entry_id)
    assert persisted["seen"] == ["already-seen", *TWENTY_FIVE]
    assert persisted["etag"] == '"v1"', "validators persist once the backlog is gone"

    # backlog drained: the next poll may be answered with a 304
    serve(aioclient_mock, status=HTTPStatus.NOT_MODIFIED)
    await restarted.async_refresh()

    assert sent_headers(aioclient_mock)["If-None-Match"] == '"v1"'
    assert emitted(restarted) == []
    assert restarted.last_update_success is True
