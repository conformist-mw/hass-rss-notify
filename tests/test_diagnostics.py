"""Tests for the config entry diagnostics and their URL redaction."""

from http import HTTPStatus
import json
from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.rss_notify.const import (
    CONF_INITIAL_ITEMS,
    CONF_MAX_ITEMS_PER_POLL,
    CONF_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
    DOMAIN,
)
from custom_components.rss_notify.diagnostics import redact_url

from .conftest import (
    FEED_LINK,
    FEED_TITLE,
    FEED_URL,
    feed_bytes,
    make_config_entry,
    seed_store,
    serve,
    serve_keys,
    setup_entry,
)

THREE_POSTS = ["post-1", "post-2", "post-3"]

# a feed behind basic auth whose query string carries an access token
SECRET_URL = "https://feeduser:s3cret@example.com/private/rss?token=t0ken&fmt=xml"
SECRET_PARTS = ("s3cret", "t0ken", "feeduser")
REDACTED_URL = (
    f"https://{REDACTED}@example.com/private/rss?token={REDACTED}&fmt={REDACTED}"
)
# the same URL as a feed's own <title>, without the `&` an XML title cannot hold
SECRET_TITLE = "https://feeduser:s3cret@example.com/private/rss?token=t0ken"
REDACTED_TITLE = f"https://{REDACTED}@example.com/private/rss?token={REDACTED}"


def secret_entry() -> MockConfigEntry:
    """Return a config entry for a feed whose URL carries credentials."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=FEED_TITLE,
        unique_id=SECRET_URL,
        data={CONF_URL: SECRET_URL, CONF_NAME: FEED_TITLE},
        options={
            CONF_UPDATE_INTERVAL: 5,
            CONF_INITIAL_ITEMS: 1,
            CONF_MAX_ITEMS_PER_POLL: 10,
        },
    )


async def diagnostics(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, entry: MockConfigEntry
) -> dict[str, Any]:
    """Return the diagnostics of one entry as the download endpoint serves them."""
    return await get_diagnostics_for_config_entry(hass, hass_client, entry)


async def test_diagnostics_report_options_feed_meta_and_state(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Diagnostics carry the options, feed meta and poll state of the feed."""
    entry = make_config_entry(**{CONF_MAX_ITEMS_PER_POLL: 2})
    # a feed synced earlier: this poll emits 2 of 3 items and holds one back
    seed_store(hass_storage, entry.entry_id, ["already-seen"], etag='"old"')
    serve_keys(aioclient_mock, THREE_POSTS, etag='"fresh"')
    await setup_entry(hass, entry)

    report = await diagnostics(hass, hass_client, entry)
    last_fetch = report.pop("last_fetch")

    assert report == {
        "options": {
            CONF_UPDATE_INTERVAL: 5,
            CONF_INITIAL_ITEMS: 1,
            CONF_MAX_ITEMS_PER_POLL: 2,
        },
        "feed": {
            "url": FEED_URL,
            "title": FEED_TITLE,
            "link": FEED_LINK,
        },
        "state": {
            "seen_count": 3,
            # the new ETag is held back while the third item is pending
            "cache_validators": {"etag": True, "last_modified": False},
            "last_batch_size": 2,
            "pending": 1,
        },
    }
    assert last_fetch["success"] is True
    assert last_fetch["error"] is None
    assert last_fetch["time"].startswith("20")


async def test_diagnostics_omit_the_seen_keys(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The seen-set is reported as a count, never as the list of item keys."""
    entry = make_config_entry()
    seed_store(hass_storage, entry.entry_id, THREE_POSTS)
    serve_keys(aioclient_mock, THREE_POSTS)
    await setup_entry(hass, entry)

    report = await diagnostics(hass, hass_client, entry)

    assert report["state"]["seen_count"] == 3
    dumped = json.dumps(report)
    assert not any(key in dumped for key in THREE_POSTS)


async def test_diagnostics_redact_the_feed_url(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Userinfo and query values of the feed URL never leave the instance."""
    entry = secret_entry()
    aioclient_mock.get(SECRET_URL, content=feed_bytes(THREE_POSTS))
    await setup_entry(hass, entry)

    report = await diagnostics(hass, hass_client, entry)

    assert report["feed"]["url"] == REDACTED_URL
    dumped = json.dumps(report)
    assert not any(secret in dumped for secret in SECRET_PARTS)


async def test_diagnostics_redact_urls_quoted_in_the_last_error(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failed poll reports its error with the URLs it quotes masked."""
    entry = secret_entry()
    aioclient_mock.get(SECRET_URL, content=feed_bytes(THREE_POSTS))
    await setup_entry(hass, entry)

    aioclient_mock.clear_requests()
    aioclient_mock.get(SECRET_URL, status=HTTPStatus.INTERNAL_SERVER_ERROR)
    await entry.runtime_data.async_refresh()

    report = await diagnostics(hass, hass_client, entry)

    assert report["last_fetch"]["success"] is False
    assert REDACTED in report["last_fetch"]["error"]
    dumped = json.dumps(report)
    assert not any(secret in dumped for secret in SECRET_PARTS)


async def test_diagnostics_redact_a_feed_title_that_is_a_url(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A feed reporting a URL as its own title does not leak it either."""
    # no configured name, so the feed's own <title> is what gets reported
    entry = make_config_entry(name=None)
    aioclient_mock.get(FEED_URL, content=feed_bytes(THREE_POSTS, title=SECRET_TITLE))
    await setup_entry(hass, entry)

    report = await diagnostics(hass, hass_client, entry)

    assert report["feed"]["title"] == REDACTED_TITLE
    dumped = json.dumps(report)
    assert not any(secret in dumped for secret in SECRET_PARTS)


async def test_diagnostics_of_a_feed_whose_first_poll_failed(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A report is downloadable while the entry is still retrying its setup.

    That is exactly the state a user reaches for diagnostics in, and the download
    endpoint does not require a loaded entry.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    serve(aioclient_mock, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY

    report = await diagnostics(hass, hass_client, entry)

    assert report["state"] == {
        "seen_count": 0,
        "cache_validators": {"etag": False, "last_modified": False},
        # no poll has produced an outcome yet
        "last_batch_size": None,
        "pending": None,
    }
    assert report["last_fetch"]["success"] is False
    assert report["last_fetch"]["time"] is None
    assert "Error fetching feed" in report["last_fetch"]["error"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", ""),
        # nothing that can be masked field by field is passed through at all
        ("http://[::1", REDACTED),
        ("mailto:feeds@example.com", REDACTED),
        ("https://example.com:8080/feed.xml", "https://example.com:8080/feed.xml"),
        ("https://example.com/feed#s3cret", "https://example.com/feed"),
        ("https://example.com/feed?bare", f"https://example.com/feed?bare={REDACTED}"),
        (
            "https://user:pw@example.com/feed?a=1&b=2",
            f"https://{REDACTED}@example.com/feed?a={REDACTED}&b={REDACTED}",
        ),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    """Every part of a URL that can carry a secret is masked."""
    assert redact_url(url) == expected
