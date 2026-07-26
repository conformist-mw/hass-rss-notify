"""Tests for the RSS Notify config and options flows."""

from collections.abc import Generator
from datetime import timedelta
from http import HTTPStatus
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
import voluptuous as vol

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

from .conftest import FEED_URL, load_feed, serve_keys, setup_entry

UNTITLED_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel>'
    b"<item><title>Only post</title><guid>only-1</guid></item>"
    b"</channel></rss>"
)
DEFAULT_OPTIONS = {
    CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
    CONF_INITIAL_ITEMS: DEFAULT_INITIAL_ITEMS,
    CONF_MAX_ITEMS_PER_POLL: DEFAULT_MAX_ITEMS_PER_POLL,
}


@pytest.fixture
def patch_setup() -> bool:
    """Whether entry setup is mocked away; parametrize to False for a real setup."""
    return True


@pytest.fixture(autouse=True)
def mock_setup_entry(patch_setup: bool) -> Generator[AsyncMock | None]:
    """Prevent the created entry from being set up for real."""
    if not patch_setup:
        yield None
        return
    with patch(
        "custom_components.rss_notify.async_setup_entry", return_value=True
    ) as mock:
        yield mock


def suggested_values(schema: vol.Schema) -> dict[str, Any]:
    """Return the values a form pre-fills, keyed by field name."""
    return {
        str(key): key.description["suggested_value"]
        for key in schema.schema
        if isinstance(key.description, dict)
    }


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
    assert result["options"] == DEFAULT_OPTIONS
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


@pytest.mark.parametrize(
    ("url", "expected_title"),
    [
        (FEED_URL, "example.com"),
        # the name becomes the entry title, the device name, the entity name and
        # the feed_title of every event: none of those may carry a credential
        (
            "https://feeduser:s3cret@private.example.com/rss?token=t0ken",
            "private.example.com",
        ),
    ],
)
async def test_user_flow_falls_back_to_the_feed_host_as_title(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    url: str,
    expected_title: str,
) -> None:
    """A feed without a title and without a name is named after its host."""
    aioclient_mock.get(url, content=UNTITLED_FEED)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_URL: url})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == expected_title
    assert result["data"][CONF_NAME] == expected_title


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


async def test_options_flow_saves_new_values(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The options form offers the values in use and stores the submitted ones."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert suggested_values(result["data_schema"]) == DEFAULT_OPTIONS

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_UPDATE_INTERVAL: 15,
            CONF_INITIAL_ITEMS: 0,
            CONF_MAX_ITEMS_PER_POLL: 0,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # a number selector yields floats; whole numbers are stored
    assert result["data"] == {
        CONF_UPDATE_INTERVAL: 15,
        CONF_INITIAL_ITEMS: 0,
        CONF_MAX_ITEMS_PER_POLL: 0,
    }
    assert all(isinstance(value, int) for value in result["data"].values())
    assert dict(mock_config_entry.options) == result["data"]


@pytest.mark.parametrize(
    "invalid_option",
    [
        {CONF_UPDATE_INTERVAL: 0},
        {CONF_INITIAL_ITEMS: -1},
        {CONF_MAX_ITEMS_PER_POLL: -1},
    ],
)
async def test_options_flow_rejects_values_below_the_minimum(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    invalid_option: dict[str, int],
) -> None:
    """Values under the field minimum are refused and change nothing."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**DEFAULT_OPTIONS, **invalid_option}
        )

    assert dict(mock_config_entry.options) == DEFAULT_OPTIONS


@pytest.mark.parametrize("patch_setup", [False])
async def test_options_flow_reload_applies_the_new_options(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Saving options reloads the feed, so its coordinator polls with them."""
    serve_keys(aioclient_mock, ["post-1", "post-2"])
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.update_interval == timedelta(minutes=DEFAULT_UPDATE_INTERVAL)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_UPDATE_INTERVAL: 15,
            CONF_INITIAL_ITEMS: 3,
            CONF_MAX_ITEMS_PER_POLL: 0,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    reloaded = mock_config_entry.runtime_data
    assert reloaded is not coordinator
    assert reloaded.update_interval == timedelta(minutes=15)
    assert reloaded.initial_items == 3
    assert reloaded.max_items_per_poll == 0
