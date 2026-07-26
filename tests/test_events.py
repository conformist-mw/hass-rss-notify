"""Tests for the entry lifecycle: bus events, entity delivery, reload, removal."""

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.const import (
    ATTR_ENTRY_ID,
    ATTR_FEED_TITLE,
    ATTR_FEED_URL,
    ATTR_ITEM_ID,
    ATTR_LINK,
    ATTR_PUBLISHED,
    ATTR_SUMMARY,
    ATTR_SUMMARY_PLAIN,
    ATTR_TITLE,
    CONF_INITIAL_ITEMS,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_NEW_ITEM,
    EVENT_TYPE_NEW_ITEM,
)
from custom_components.rss_notify.storage import storage_key

from .conftest import (
    FEED_TITLE,
    FEED_URL,
    event_entity_id,
    make_config_entry,
    seed_store,
    serve_keys,
    setup_entry,
)

THREE_POSTS = ["post-1", "post-2", "post-3"]


async def test_bus_events_fired_per_item_in_order(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Every emitted item fires one bus event, oldest first, with full payload."""
    entry = make_config_entry()
    # a feed synced earlier, so all three items are emitted by this poll
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve_keys(aioclient_mock, THREE_POSTS)
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    await setup_entry(hass, entry)

    assert [event.data[ATTR_ITEM_ID] for event in events] == THREE_POSTS
    assert events[0].data == {
        ATTR_ENTRY_ID: entry.entry_id,
        ATTR_FEED_URL: FEED_URL,
        ATTR_FEED_TITLE: FEED_TITLE,
        ATTR_ITEM_ID: "post-1",
        ATTR_TITLE: "Post post-1",
        ATTR_LINK: "https://example.com/posts/post-1",
        ATTR_SUMMARY: "Body of post-1",
        ATTR_SUMMARY_PLAIN: "Body of post-1",
        ATTR_PUBLISHED: "2026-07-01T12:00:00+00:00",
    }


async def test_initial_items_reach_the_event_entity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """On the very first setup the initial item reaches the entity and the bus."""
    entry = make_config_entry()
    serve_keys(aioclient_mock, THREE_POSTS)
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert [event.data[ATTR_ITEM_ID] for event in events] == ["post-3"]

    state = hass.states.get(event_entity_id(hass, entry))
    assert state is not None
    assert state.state != STATE_UNKNOWN
    assert state.attributes["event_type"] == EVENT_TYPE_NEW_ITEM
    assert state.attributes[ATTR_ITEM_ID] == "post-3"
    assert state.attributes[ATTR_TITLE] == "Post post-3"


async def test_silent_initial_sync_emits_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """`initial_items` of 0 sets the feed up without firing a single event."""
    entry = make_config_entry(**{CONF_INITIAL_ITEMS: 0})
    serve_keys(aioclient_mock, THREE_POSTS)
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    await setup_entry(hass, entry)

    assert events == []
    assert hass.states.get(event_entity_id(hass, entry)).state == STATE_UNKNOWN


async def test_entry_registers_a_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The feed gets a service device the event entity is attached to."""
    entry = make_config_entry()
    serve_keys(aioclient_mock, THREE_POSTS)

    await setup_entry(hass, entry)

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.name == FEED_TITLE
    assert device.configuration_url == FEED_URL
    assert device.entry_type is dr.DeviceEntryType.SERVICE

    entity = er.async_get(hass).async_get(event_entity_id(hass, entry))
    assert entity is not None
    assert entity.device_id == device.id


async def test_options_change_reloads_the_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Changing options reloads the entry, so the coordinator picks them up."""
    entry = make_config_entry()
    serve_keys(aioclient_mock, THREE_POSTS)
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_UPDATE_INTERVAL: 30}
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not coordinator
    assert entry.runtime_data.update_interval.total_seconds() == 30 * 60


async def test_removing_the_entry_deletes_the_stored_state(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Removing a feed unloads it and deletes its storage file."""
    entry = make_config_entry()
    serve_keys(aioclient_mock, THREE_POSTS)
    await setup_entry(hass, entry)
    assert storage_key(entry.entry_id) in hass_storage

    assert await hass.config_entries.async_remove(entry.entry_id) == {
        "require_restart": False
    }
    await hass.async_block_till_done()

    assert storage_key(entry.entry_id) not in hass_storage
    assert hass.states.async_entity_ids(Platform.EVENT) == []
