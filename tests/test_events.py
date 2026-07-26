"""Tests for the entry lifecycle: bus events, entity delivery, reload, removal."""

from typing import Any

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
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
    OTHER_FEED_TITLE,
    OTHER_FEED_URL,
    event_entity_id,
    feed_bytes,
    make_config_entry,
    seed_store,
    serve_keys,
    setup_entry,
    stored,
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


async def test_event_contract_uses_the_documented_names(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The bus event type and the payload keys are the documented literals.

    These strings are the whole consumer API: every user automation matches on
    them, so they are spelled out here instead of imported from `const.py`, where
    a rename would silently take the tests with it.
    """
    entry = make_config_entry()
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve_keys(aioclient_mock, ["post-1"])
    events = async_capture_events(hass, "rss_notify_new_item")

    await setup_entry(hass, entry)

    assert len(events) == 1
    assert events[0].event_type == "rss_notify_new_item"
    assert set(events[0].data) == {
        "entry_id",
        "feed_url",
        "feed_title",
        "item_id",
        "title",
        "link",
        "summary",
        "summary_plain",
        "published",
    }
    assert events[0].data["item_id"] == "post-1"

    # the event entity offers the same item under the same names
    attributes = hass.states.get(event_entity_id(hass, entry)).attributes
    assert attributes["event_type"] == "new_item"
    assert attributes["item_id"] == "post-1"
    assert attributes["summary_plain"] == "Body of post-1"


async def test_two_feeds_stay_independent(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """One config entry per feed: events, entities and storage stay separate."""
    first = make_config_entry()
    second = make_config_entry(name=OTHER_FEED_TITLE, url=OTHER_FEED_URL)
    aioclient_mock.get(FEED_URL, content=feed_bytes(["post-1"]))
    aioclient_mock.get(
        OTHER_FEED_URL, content=feed_bytes(["other-1"], title=OTHER_FEED_TITLE)
    )
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    await setup_entry(hass, first)
    await setup_entry(hass, second)

    assert first.entry_id != second.entry_id
    assert [
        (event.data[ATTR_ENTRY_ID], event.data[ATTR_FEED_URL], event.data[ATTR_ITEM_ID])
        for event in events
    ] == [
        (first.entry_id, FEED_URL, "post-1"),
        (second.entry_id, OTHER_FEED_URL, "other-1"),
    ]

    # two devices, two entities, each carrying only its own feed's item
    registry = er.async_get(hass)
    first_entity = event_entity_id(hass, first)
    second_entity = event_entity_id(hass, second)
    assert first_entity != second_entity
    assert registry.async_get(first_entity).device_id != (
        registry.async_get(second_entity).device_id
    )
    assert hass.states.get(first_entity).attributes[ATTR_FEED_TITLE] == FEED_TITLE
    assert hass.states.get(second_entity).attributes[ATTR_FEED_TITLE] == (
        OTHER_FEED_TITLE
    )

    # the seen-sets do not know about each other
    assert stored(hass_storage, first.entry_id)["seen"] == ["post-1"]
    assert stored(hass_storage, second.entry_id)["seen"] == ["other-1"]


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


async def test_disabling_the_entry_pauses_polling_without_reemitting(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Disabling a feed stops it; re-enabling announces only what came in between."""
    entry = make_config_entry()
    serve_keys(aioclient_mock, THREE_POSTS)
    events = async_capture_events(hass, EVENT_NEW_ITEM)
    await setup_entry(hass, entry)
    assert [event.data[ATTR_ITEM_ID] for event in events] == ["post-3"]

    # pause: HA unloads the entry, so nothing is polled while it is disabled
    assert await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()

    entity_id = event_entity_id(hass, entry)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert er.async_get(hass).async_get(entity_id).disabled_by is (
        er.RegistryEntryDisabler.CONFIG_ENTRY
    )
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    # the feed publishes two items while the entry is paused
    serve_keys(aioclient_mock, [*THREE_POSTS, "post-4", "post-5"])
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert er.async_get(hass).async_get(entity_id).disabled_by is None
    # the seen-set survived the pause: only the two new items are announced
    assert [event.data[ATTR_ITEM_ID] for event in events] == [
        "post-3",
        "post-4",
        "post-5",
    ]
    assert hass.states.get(entity_id).attributes[ATTR_ITEM_ID] == "post-5"


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
