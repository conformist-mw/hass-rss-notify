"""Tests for the RSS Notify config flow."""

from collections.abc import Generator
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.client import NotModified
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
)

from .conftest import FEED_URL, load_feed

UNTITLED_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel>'
    b"<item><title>Only post</title><guid>only-1</guid></item>"
    b"</channel></rss>"
)


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Prevent the created entry from being set up for real."""
    with patch(
        "custom_components.rss_notify.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def _start_flow(hass: HomeAssistant) -> str:
    """Start the user flow and return its id, asserting the form is shown."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]
    return result["flow_id"]


async def test_user_flow_creates_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_setup_entry: AsyncMock,
) -> None:
    """A valid feed creates an entry titled after the feed, with default options."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_URL: f"  {FEED_URL}  "}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Example Blog"
    assert result["data"] == {CONF_URL: FEED_URL, CONF_NAME: "Example Blog"}
    assert result["options"] == {
        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
        CONF_INITIAL_ITEMS: DEFAULT_INITIAL_ITEMS,
        CONF_MAX_ITEMS_PER_POLL: DEFAULT_MAX_ITEMS_PER_POLL,
    }
    assert result["result"].unique_id == FEED_URL
    assert len(mock_setup_entry.mock_calls) == 1


async def test_user_flow_custom_name_wins(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A name entered by the user overrides the feed title."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_URL: FEED_URL, CONF_NAME: "  My feed  "}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My feed"
    assert result["data"][CONF_NAME] == "My feed"


async def test_user_flow_falls_back_to_url_as_title(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A feed without a title and without a name is titled by its URL."""
    aioclient_mock.get(FEED_URL, content=UNTITLED_FEED)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_URL: FEED_URL}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == FEED_URL


async def test_duplicate_feed_aborts(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Adding an already configured feed URL aborts without fetching it."""
    mock_config_entry.add_to_hass(hass)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_URL: FEED_URL}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert not aioclient_mock.mock_calls


@pytest.mark.parametrize(
    ("mock_kwargs", "expected_error"),
    [
        ({"exc": aiohttp.ClientError("boom")}, "cannot_connect"),
        ({"status": HTTPStatus.INTERNAL_SERVER_ERROR}, "cannot_connect"),
        ({"exc": TimeoutError()}, "cannot_connect"),
        ({"content": b"<html><body>not a feed</body></html>"}, "invalid_feed"),
    ],
)
async def test_flow_errors_recover(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_kwargs: dict[str, object],
    expected_error: str,
) -> None:
    """A failing feed re-shows the form with an error and recovers afterwards."""
    aioclient_mock.get(FEED_URL, **mock_kwargs)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_URL: FEED_URL}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected_error}

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_URL: FEED_URL}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Example Blog"


async def test_unexpected_not_modified_is_invalid_feed(hass: HomeAssistant) -> None:
    """A 'not modified' answer to the unconditional validation fetch is an error."""
    flow_id = await _start_flow(hass)

    with patch(
        "custom_components.rss_notify.config_flow.async_fetch_feed",
        return_value=NotModified(),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_URL: FEED_URL}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_feed"}
