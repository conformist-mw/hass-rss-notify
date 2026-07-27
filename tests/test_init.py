"""Tests for setting an entry up, retrying a failed setup and unloading it.

Everything a *working* entry then does - bus events, entity delivery, reload on a
rename or an options change, removal - lives in `test_events.py`.
"""

from datetime import timedelta
from typing import Any

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.const import ATTR_ITEM_ID, DEFAULT_UPDATE_INTERVAL
from custom_components.rss_notify.coordinator import RssFeedCoordinator

from .conftest import FEED_URL, event_entity_id, serve, serve_keys, stored


async def test_setup_and_unload_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A config entry sets up its feed and unloads cleanly."""
    mock_config_entry.add_to_hass(hass)
    serve_keys(aioclient_mock, ["post-1", "post-2"])

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert isinstance(mock_config_entry.runtime_data, RssFeedCoordinator)
    assert mock_config_entry.runtime_data.url == FEED_URL
    assert hass.states.async_entity_ids(Platform.EVENT)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    # the entity survives in the registry as a restored, unavailable state
    entity_id = hass.states.async_entity_ids(Platform.EVENT)[0]
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


async def test_setup_retries_when_the_feed_is_unreachable(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A failing first poll leaves the entry in retry instead of loaded."""
    mock_config_entry.add_to_hass(hass)
    serve(aioclient_mock, status=500)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_retry_after_an_unreachable_feed_loads_and_keeps_polling(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A retry after a failed first poll produces a working, polling feed.

    The event platform is forwarded before the first refresh, and HA leaves a
    forwarded platform in place when the setup fails, so the retry must not end
    up with an entity bound to the coordinator of the failed attempt.
    """
    mock_config_entry.add_to_hass(hass)
    # HA starts before the network is up: the very first poll cannot reach it
    serve(aioclient_mock, exc=aiohttp.ClientError("network is unreachable"))

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY

    # the network is back and HA retries the setup
    serve_keys(aioclient_mock, ["post-1", "post-2"])
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    entity_id = event_entity_id(hass, mock_config_entry)
    state = hass.states.get(entity_id)
    assert state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    assert state.attributes[ATTR_ITEM_ID] == "post-2"
    assert stored(hass_storage, mock_config_entry.entry_id)["seen"] == [
        "post-1",
        "post-2",
    ]

    # the coordinator of the retry is scheduled, so the feed keeps being polled
    serve_keys(aioclient_mock, ["post-1", "post-2", "post-3"])
    freezer.tick(timedelta(minutes=DEFAULT_UPDATE_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(entity_id).attributes[ATTR_ITEM_ID] == "post-3"
