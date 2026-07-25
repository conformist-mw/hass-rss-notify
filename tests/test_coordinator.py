"""Tests for the feed coordinator: dedup, ordering, initial sync and trickle."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from email.utils import format_datetime
from http import HTTPStatus
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.const import (
    CONF_INITIAL_ITEMS,
    CONF_MAX_ITEMS_PER_POLL,
    STORAGE_VERSION,
)
from custom_components.rss_notify.coordinator import RssFeedCoordinator
from custom_components.rss_notify.storage import SeenStore, storage_key

from .conftest import FEED_URL, load_feed, make_config_entry

FEED_TITLE = "Example Blog"
BASE_PUBLISHED = datetime(2026, 7, 1, 12, 0, tzinfo=dt_util.UTC)

TWENTY_FIVE = [f"post-{index}" for index in range(1, 26)]


def feed_bytes(keys: Sequence[str], title: str = FEED_TITLE) -> bytes:
    """Build an RSS feed for `keys` (oldest first), served newest first."""
    items = [
        "<item>"
        f"<title>Post {key}</title>"
        f"<link>https://example.com/posts/{key}</link>"
        f'<guid isPermaLink="false">{key}</guid>'
        f"<description>Body of {key}</description>"
        f"<pubDate>{format_datetime(BASE_PUBLISHED + timedelta(days=index))}</pubDate>"
        "</item>"
        for index, key in enumerate(keys)
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{title}</title><link>https://example.com/</link>"
        f"{''.join(reversed(items))}"
        "</channel></rss>"
    ).encode()


def serve(
    aioclient_mock: AiohttpClientMocker,
    *,
    content: bytes | None = None,
    status: HTTPStatus = HTTPStatus.OK,
    etag: str | None = None,
    exc: Exception | None = None,
) -> None:
    """Register the single response the next poll will receive."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        FEED_URL,
        content=content,
        status=status,
        headers={"ETag": etag} if etag else None,
        exc=exc,
    )


def serve_keys(
    aioclient_mock: AiohttpClientMocker,
    keys: Sequence[str],
    etag: str | None = None,
) -> None:
    """Serve a generated feed holding `keys` on the next poll."""
    serve(aioclient_mock, content=feed_bytes(keys), etag=etag)


def seed_store(
    hass_storage: dict[str, Any],
    entry_id: str,
    seen: list[str],
    etag: str | None = None,
) -> None:
    """Pre-populate the persisted state of a feed, as an earlier run would."""
    key = storage_key(entry_id)
    hass_storage[key] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": key,
        "data": {"seen": seen, "etag": etag, "last_modified": None},
    }


def stored(hass_storage: dict[str, Any], entry_id: str) -> dict[str, Any]:
    """Return the persisted state of a feed."""
    return hass_storage[storage_key(entry_id)]["data"]


def sent_headers(aioclient_mock: AiohttpClientMocker) -> dict[str, str]:
    """Return the headers of the most recent request."""
    return aioclient_mock.mock_calls[-1][3]


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
    assert coordinator.data.feed_link == "https://example.com/"

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
    assert coordinator.data.feed_link == "https://example.com/"
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
