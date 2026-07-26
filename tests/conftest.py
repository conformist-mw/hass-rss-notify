"""Fixtures for the RSS Notify tests."""

from collections.abc import Generator, Sequence
from datetime import datetime, timedelta
from email.utils import format_datetime
from http import HTTPStatus
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    load_fixture_bytes,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.const import (
    CONF_INITIAL_ITEMS,
    CONF_MAX_ITEMS_PER_POLL,
    CONF_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
    DEFAULT_INITIAL_ITEMS,
    DEFAULT_MAX_ITEMS_PER_POLL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_TYPE_NEW_ITEM,
    STORAGE_VERSION,
)
from custom_components.rss_notify.storage import storage_key

FEED_URL = "https://example.com/rss"
FEED_TITLE = "Example Blog"
FEED_LINK = "https://example.com/"
BASE_PUBLISHED = datetime(2026, 7, 1, 12, 0, tzinfo=dt_util.UTC)


def load_feed(name: str) -> bytes:
    """Return the raw bytes of a feed fixture from `tests/fixtures`."""
    return load_fixture_bytes(f"{name}.xml")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading of custom integrations in all tests."""
    yield


def make_config_entry(
    *, name: str | None = FEED_TITLE, **options: int
) -> MockConfigEntry:
    """Return a config entry for one feed, with default options plus overrides.

    `name` is the name configured for the feed; `None` leaves it unset, as an
    entry created before the config flow started storing one would be.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        title=name or FEED_URL,
        unique_id=FEED_URL,
        data={CONF_URL: FEED_URL} | ({CONF_NAME: name} if name is not None else {}),
        options={
            CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
            CONF_INITIAL_ITEMS: DEFAULT_INITIAL_ITEMS,
            CONF_MAX_ITEMS_PER_POLL: DEFAULT_MAX_ITEMS_PER_POLL,
            **options,
        },
    )


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for a single feed with default options."""
    return make_config_entry()


def feed_bytes(keys: Sequence[str], title: str = FEED_TITLE, body: str = "") -> bytes:
    """Build an RSS feed for `keys` (oldest first), served newest first."""
    items = [
        "<item>"
        f"<title>Post {key}</title>"
        f"<link>https://example.com/posts/{key}</link>"
        f'<guid isPermaLink="false">{key}</guid>'
        f"<description>{body or f'Body of {key}'}</description>"
        f"<pubDate>{format_datetime(BASE_PUBLISHED + timedelta(days=index))}</pubDate>"
        "</item>"
        for index, key in enumerate(keys)
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{title}</title><link>{FEED_LINK}</link>"
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


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry to hass and set it up, as the UI would."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def event_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity id of the event entity belonging to `entry`."""
    entity_id = er.async_get(hass).async_get_entity_id(
        Platform.EVENT, DOMAIN, f"{entry.entry_id}_{EVENT_TYPE_NEW_ITEM}"
    )
    assert entity_id is not None
    return entity_id
