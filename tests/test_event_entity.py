"""Tests for the per-feed event entity: naming, truncation, availability."""

from datetime import timedelta
from http import HTTPStatus
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.const import (
    ATTR_ITEM_ID,
    ATTR_MAX_LENGTH,
    ATTR_SUMMARY,
    ATTR_SUMMARY_PLAIN,
    ATTR_TITLE,
    CONF_INITIAL_ITEMS,
    DEFAULT_UPDATE_INTERVAL,
    EVENT_NEW_ITEM,
    EVENT_TYPE_NEW_ITEM,
)

from .conftest import (
    FEED_TITLE,
    event_entity_id,
    feed_bytes,
    make_config_entry,
    seed_store,
    serve,
    serve_keys,
    setup_entry,
    stored,
)

FIVE_POSTS = ["post-1", "post-2", "post-3", "post-4", "post-5"]
# well past the 500-char attribute cap
LONG_BODY = "Lorem ipsum dolor sit amet. " * 40


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Run one coordinator poll of the feed and let the entity catch up."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def state_changes(events: list[Event], entity_id: str) -> list[str]:
    """Return the item ids of the state changes of one entity, in order."""
    return [
        event.data["new_state"].attributes[ATTR_ITEM_ID]
        for event in events
        if event.data["entity_id"] == entity_id
    ]


async def test_entity_is_created_and_named_after_its_feed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """One entity per feed, named after the feed and offering `new_item`."""
    entry = make_config_entry()
    serve_keys(aioclient_mock, FIVE_POSTS)

    await setup_entry(hass, entry)

    entity_id = event_entity_id(hass, entry)
    assert entity_id == "event.example_blog_new_item"

    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None
    assert entity.unique_id == f"{entry.entry_id}_{EVENT_TYPE_NEW_ITEM}"
    assert entity.device_id is not None

    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_FRIENDLY_NAME] == f"{FEED_TITLE} New item"
    assert state.attributes["event_types"] == [EVENT_TYPE_NEW_ITEM]


async def test_item_text_is_truncated_in_the_attributes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Entity attributes cap the item text, while the bus event keeps it whole."""
    entry = make_config_entry()
    # a feed synced earlier, so the single item is emitted by this poll
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve(aioclient_mock, content=feed_bytes(["post-1"], body=LONG_BODY))
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    await setup_entry(hass, entry)

    full = events[0].data
    assert len(full[ATTR_SUMMARY]) > ATTR_MAX_LENGTH
    assert len(full[ATTR_SUMMARY_PLAIN]) > ATTR_MAX_LENGTH

    attributes = hass.states.get(event_entity_id(hass, entry)).attributes
    assert attributes[ATTR_SUMMARY] == full[ATTR_SUMMARY][:ATTR_MAX_LENGTH]
    assert attributes[ATTR_SUMMARY_PLAIN] == full[ATTR_SUMMARY_PLAIN][:ATTR_MAX_LENGTH]
    # the documented cap, spelled out: importing it would assert it against itself
    assert len(attributes[ATTR_SUMMARY]) == 500
    # short fields are passed through untouched
    assert attributes[ATTR_TITLE] == full[ATTR_TITLE]
    assert attributes[ATTR_ITEM_ID] == "post-1"


async def test_batch_produces_one_state_change_per_item(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A batch of N items writes state N times, oldest item first."""
    entry = make_config_entry(**{CONF_INITIAL_ITEMS: 0})
    serve_keys(aioclient_mock, ["post-0"])
    await setup_entry(hass, entry)
    entity_id = event_entity_id(hass, entry)
    assert hass.states.get(entity_id).state == STATE_UNKNOWN

    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    serve_keys(aioclient_mock, ["post-0", *FIVE_POSTS])
    await poll(hass, entry)

    # every item is its own state change, not just the newest one
    assert state_changes(changes, entity_id) == FIVE_POSTS
    assert hass.states.get(entity_id).attributes[ATTR_ITEM_ID] == "post-5"


async def test_batch_after_a_failed_poll_still_writes_every_item(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Recovering from a failed poll delivers every item, not just the last one.

    Entity availability follows the coordinator's `last_update_success`, which
    is still False while the recovery poll runs. Without being marked successful
    before the batch is published, every per-item write would land as
    `unavailable` and the items would never reach the entity.
    """
    entry = make_config_entry(**{CONF_INITIAL_ITEMS: 0})
    serve_keys(aioclient_mock, ["post-0"])
    await setup_entry(hass, entry)
    entity_id = event_entity_id(hass, entry)

    serve(aioclient_mock, status=HTTPStatus.INTERNAL_SERVER_ERROR)
    await poll(hass, entry)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    bus_events = async_capture_events(hass, EVENT_NEW_ITEM)
    serve_keys(aioclient_mock, ["post-0", "post-1", "post-2", "post-3"])
    await poll(hass, entry)

    assert [event.data[ATTR_ITEM_ID] for event in bus_events] == [
        "post-1",
        "post-2",
        "post-3",
    ]
    # one state change per item, each carrying its own item, none unavailable
    assert state_changes(changes, entity_id) == ["post-1", "post-2", "post-3"]
    assert hass.states.get(entity_id).attributes[ATTR_ITEM_ID] == "post-3"


async def test_the_update_interval_drives_a_real_poll(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Polling happens on its own: the scheduled refresh emits and persists."""
    entry = make_config_entry()
    serve_keys(aioclient_mock, ["post-1"])
    await setup_entry(hass, entry)
    entity_id = event_entity_id(hass, entry)
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    serve_keys(aioclient_mock, ["post-1", "post-2"])
    freezer.tick(timedelta(minutes=DEFAULT_UPDATE_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [event.data[ATTR_ITEM_ID] for event in events] == ["post-2"]
    assert hass.states.get(entity_id).attributes[ATTR_ITEM_ID] == "post-2"
    assert stored(hass_storage, entry.entry_id)["seen"] == ["post-1", "post-2"]


async def test_entity_unavailable_while_the_feed_cannot_be_polled(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A failing poll makes the entity unavailable until the feed is back."""
    entry = make_config_entry()
    serve_keys(aioclient_mock, FIVE_POSTS)
    await setup_entry(hass, entry)
    entity_id = event_entity_id(hass, entry)
    last_event_state = hass.states.get(entity_id).state
    assert last_event_state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)

    serve(aioclient_mock, status=HTTPStatus.NOT_FOUND)
    await poll(hass, entry)
    assert entry.runtime_data.last_update_success is False
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    serve_keys(aioclient_mock, FIVE_POSTS)
    await poll(hass, entry)
    assert entry.runtime_data.last_update_success is True
    # recovering keeps the last item, no event is replayed
    assert hass.states.get(entity_id).state == last_event_state
    assert hass.states.get(entity_id).attributes[ATTR_ITEM_ID] == "post-5"
