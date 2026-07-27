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
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.const import (
    ATTR_ITEM_ID,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_NEW_ITEM,
    EVENT_TYPE_NEW_ITEM,
)
from custom_components.rss_notify.coordinator import RssFeedCoordinator

from .conftest import (
    FEED_URL,
    event_entity_id,
    serve,
    serve_keys,
    setup_entry,
    stored,
)


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


async def test_polling_survives_a_disabled_event_entity(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A feed whose entity is disabled keeps polling and keeps firing bus events.

    `DataUpdateCoordinator` schedules a refresh only while it has a listener, and
    the event entity is the only thing that registers one - from
    `CoordinatorEntity.async_added_to_hass`, which never runs for an entity
    disabled in the registry. Without the entry-level keep-alive listener such a
    feed performed its initial sync and was then never polled again, taking the
    bus surface down with it silently. Disabling the entity to keep the recorder
    clean is a documented, supported thing to do.
    """
    mock_config_entry.add_to_hass(hass)
    entity_registry.async_get_or_create(
        Platform.EVENT,
        DOMAIN,
        f"{mock_config_entry.entry_id}_{EVENT_TYPE_NEW_ITEM}",
        config_entry=mock_config_entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    serve_keys(aioclient_mock, ["post-1"])
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.states.async_entity_ids(Platform.EVENT), "the entity is disabled"
    assert [event.data[ATTR_ITEM_ID] for event in events] == ["post-1"]

    serve_keys(aioclient_mock, ["post-1", "post-2"])
    freezer.tick(timedelta(minutes=DEFAULT_UPDATE_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [event.data[ATTR_ITEM_ID] for event in events] == ["post-1", "post-2"]


async def test_the_keep_alive_listener_neither_double_polls_nor_leaks(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With the entity enabled the keep-alive adds no second poll, and unload stops it.

    The entity registers a listener of its own, so the entry's keep-alive must not
    make the interval fire twice - and it must be removed on unload, or the timer
    would outlive the entry.
    """
    serve_keys(aioclient_mock, ["post-1"])
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    # the entity's listener plus the entry's keep-alive
    assert len(coordinator._listeners) == 2

    serve_keys(aioclient_mock, ["post-1", "post-2"])
    polls_before = len(aioclient_mock.mock_calls)
    freezer.tick(timedelta(minutes=DEFAULT_UPDATE_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(aioclient_mock.mock_calls) - polls_before == 1

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert not coordinator._listeners
    assert coordinator._unsub_refresh is None

    serve_keys(aioclient_mock, ["post-1", "post-2", "post-3"])
    polls_before = len(aioclient_mock.mock_calls)
    freezer.tick(timedelta(minutes=DEFAULT_UPDATE_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(aioclient_mock.mock_calls) - polls_before == 0
