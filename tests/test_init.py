"""Tests for the RSS Notify entry lifecycle."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.coordinator import RssFeedCoordinator

from .conftest import FEED_URL, serve, serve_keys


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
