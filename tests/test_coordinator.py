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
from custom_components.rss_notify.storage import SeenStore

from .conftest import (
    FEED_LINK,
    FEED_TITLE,
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
    assert coordinator.data.feed_title == FEED_TITLE
    assert coordinator.data.feed_link == FEED_LINK

    # every item of the feed is marked seen, emitted or not
    persisted = stored(hass_storage, entry.entry_id)
    assert persisted["seen"] == ["post-1", "post-2", "post-3"]
    assert persisted["etag"] == '"v1"'
    assert coordinator.seen_count == 3


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
    """New items are emitted oldest to newest, undated items first."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    # a feed that was synced before, so this poll takes the steady-state path
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve(aioclient_mock, content=load_feed("feed_no_dates"))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert emitted(coordinator) == ["undated-a", "undated-c", "dated-d", "dated-b"]
    assert stored(hass_storage, entry.entry_id)["seen"] == [
        "already-seen",
        "undated-a",
        "undated-c",
        "dated-d",
        "dated-b",
    ]


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
    assert coordinator.seen_count == 2


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


async def test_configured_name_stays_the_reported_feed_title(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A name configured for the feed is not overwritten by the feed's title."""
    entry = make_config_entry(name="Work RSS")
    entry.add_to_hass(hass)
    serve(aioclient_mock, content=feed_bytes(["post-1"], title="Example Blog"))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert coordinator.feed_title == "Work RSS"
    assert coordinator.data.feed_title == "Work RSS"


async def test_feed_title_is_discovered_when_no_name_is_configured(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without a configured name the feed's own title is adopted."""
    entry = make_config_entry(name=None)
    entry.add_to_hass(hass)
    serve(aioclient_mock, content=feed_bytes(["post-1"], title="Discovered Blog"))

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert coordinator.feed_title == "Discovered Blog"
    assert coordinator.data.feed_title == "Discovered Blog"


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
    assert coordinator.data.feed_title == FEED_TITLE
    assert coordinator.data.feed_link == FEED_LINK
    assert stored(hass_storage, entry.entry_id) == before


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


async def test_backlog_keeps_the_previous_validators(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """With a backlog pending, the old validators are kept, not the new ones."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    seed_store(hass_storage, entry.entry_id, ["already-seen"], etag='"v0"')
    serve_keys(aioclient_mock, TWENTY_FIVE, etag='"v1"')

    coordinator = await build_coordinator(hass, entry)
    await coordinator.async_refresh()

    assert sent_headers(aioclient_mock)["If-None-Match"] == '"v0"'
    assert coordinator.data.pending == 15
    assert stored(hass_storage, entry.entry_id)["etag"] == '"v0"'


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
