"""Tests for the config entry diagnostics report."""

from http import HTTPStatus
import json
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import REDACTED
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.rss_notify.const import (
    CONF_MAX_ITEMS_PER_POLL,
)

from .conftest import (
    DEFAULT_OPTIONS,
    FEED_LINK,
    FEED_TITLE,
    FEED_URL,
    SECRET_PARTS,
    SECRET_URL,
    THREE_POSTS,
    feed_bytes,
    make_config_entry,
    seed_store,
    serve,
    serve_keys,
    setup_entry,
)

REDACTED_URL = (
    f"https://{REDACTED}@example.com/private/rss?token={REDACTED}&fmt={REDACTED}"
)
# the same URL as a feed's own <title>, without the `&` an XML title cannot hold
SECRET_TITLE = "https://feeduser:s3cret@example.com/private/rss?token=t0ken"
REDACTED_TITLE = f"https://{REDACTED}@example.com/private/rss?token={REDACTED}"
# a feed whose <link> - not its URL - is where the secret sits
SECRET_LINK = "https://example.com/private?key=s3cret"


def secret_entry() -> MockConfigEntry:
    """Return a config entry for a feed whose URL carries credentials."""
    return make_config_entry(url=SECRET_URL)


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
        "options": {**DEFAULT_OPTIONS, CONF_MAX_ITEMS_PER_POLL: 2},
        "feed": {
            "url": FEED_URL,
            "title": FEED_TITLE,
            "link": FEED_LINK,
        },
        "state": {
            "seen_count": 3,
            # the validators are cleared while the third item is pending
            "cache_validators": {"etag": False, "last_modified": False},
            "last_batch_size": 2,
            "pending": 1,
        },
    }
    assert last_fetch["success"] is True
    assert last_fetch["error"] is None
    assert dt_util.parse_datetime(last_fetch["time"]) is not None


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
    serve(aioclient_mock, url=SECRET_URL, content=feed_bytes(THREE_POSTS))
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
    serve(aioclient_mock, url=SECRET_URL, content=feed_bytes(THREE_POSTS))
    await setup_entry(hass, entry)

    serve(aioclient_mock, url=SECRET_URL, status=HTTPStatus.INTERNAL_SERVER_ERROR)
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
    """A feed name that is a URL does not leak it either.

    The config flow names a feed after its own `<title>` when the user gives no
    name, and that title may well be a URL with a token in it.
    """
    entry = make_config_entry(title=SECRET_TITLE)
    serve(aioclient_mock, content=feed_bytes(THREE_POSTS))
    await setup_entry(hass, entry)

    report = await diagnostics(hass, hass_client, entry)

    assert report["feed"]["title"] == REDACTED_TITLE
    dumped = json.dumps(report)
    assert not any(secret in dumped for secret in SECRET_PARTS)


async def test_diagnostics_redact_the_feed_link(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The site link the feed reports is masked like the feed URL itself."""
    entry = make_config_entry()
    body = feed_bytes(THREE_POSTS).replace(
        f"<link>{FEED_LINK}</link>".encode(), f"<link>{SECRET_LINK}</link>".encode()
    )
    serve(aioclient_mock, content=body)
    await setup_entry(hass, entry)

    report = await diagnostics(hass, hass_client, entry)

    assert report["feed"]["link"] == f"https://example.com/private?key={REDACTED}"
    assert "s3cret" not in json.dumps(report)


async def test_diagnostics_do_not_report_the_error_of_a_recovered_poll(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A successful poll clears the reported error.

    The coordinator never clears `last_exception`, so reporting it unconditionally
    would pair `success: true` with the error text of a poll that failed hours ago.
    """
    entry = make_config_entry()
    serve_keys(aioclient_mock, THREE_POSTS)
    await setup_entry(hass, entry)

    serve(aioclient_mock, status=HTTPStatus.INTERNAL_SERVER_ERROR)
    await entry.runtime_data.async_refresh()
    assert (await diagnostics(hass, hass_client, entry))["last_fetch"]["error"]

    serve_keys(aioclient_mock, THREE_POSTS)
    await entry.runtime_data.async_refresh()

    last_fetch = (await diagnostics(hass, hass_client, entry))["last_fetch"]
    assert last_fetch["success"] is True
    assert last_fetch["error"] is None


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
