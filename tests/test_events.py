"""Tests for what a loaded entry publishes and how it survives being changed.

The bus payload, the delivery to the event entity, the device the feed is grouped
under, the notification the README's documented template builds out of a payload,
and the transitions that reload, pause or remove a working entry. Plain setup, the
setup retry and unload live in `test_init.py`.
"""

from pathlib import Path
import re
from typing import Any

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.template import Template
import pytest
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
import yaml

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
    THREE_POSTS,
    event_entity_id,
    feed_bytes,
    load_feed,
    make_config_entry,
    seed_store,
    serve,
    serve_keys,
    setup_entry,
    stored,
)


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
    second = make_config_entry(title=OTHER_FEED_TITLE, url=OTHER_FEED_URL)
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    serve(aioclient_mock, content=feed_bytes(["post-1"]))
    await setup_entry(hass, first)
    serve(
        aioclient_mock,
        url=OTHER_FEED_URL,
        content=feed_bytes(["other-1"], title=OTHER_FEED_TITLE),
    )
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


async def test_an_undated_item_reports_no_publication_time(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """An item the feed gives no date for is announced with `published: null`."""
    entry = make_config_entry()
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve(aioclient_mock, content=load_feed("feed_undated"))
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    await setup_entry(hass, entry)

    assert [event.data[ATTR_PUBLISHED] for event in events] == [None, None, None]
    attributes = hass.states.get(event_entity_id(hass, entry)).attributes
    assert attributes[ATTR_PUBLISHED] is None


async def test_renaming_the_feed_updates_its_name_everywhere(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Renaming the entry reloads the feed and renames device, entity and payload.

    The feed's name lives in the entry title only, so a rename is picked up by
    the reload the update listener triggers - and cannot leave the device, the
    entity and the announced `feed_title` disagreeing.
    """
    entry = make_config_entry()
    serve_keys(aioclient_mock, THREE_POSTS)
    await setup_entry(hass, entry)
    entity_id = event_entity_id(hass, entry)
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    serve_keys(aioclient_mock, [*THREE_POSTS, "post-4"])
    hass.config_entries.async_update_entry(entry, title="Renamed Blog")
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.feed_title == "Renamed Blog"
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device.name == "Renamed Blog"
    assert hass.states.get(entity_id).attributes[ATTR_FRIENDLY_NAME] == (
        "Renamed Blog New item"
    )
    # the reload keeps the seen-set, so only the new item is announced - under
    # the new name
    assert [
        (event.data[ATTR_ITEM_ID], event.data[ATTR_FEED_TITLE]) for event in events
    ] == [("post-4", "Renamed Blog")]


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


def _readme_telegram_message() -> str:
    """Return the message template of the README's Telegram example.

    Read out of the README instead of transcribed here: this guards the
    documented example against losing the item's own description again, and a
    copy kept in the test file would keep passing while the README drifted.
    """
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    examples = [
        yaml.safe_load(block)
        for block in re.findall(r"```yaml\n(.*?)```", readme, re.DOTALL)
        if "RSS to Telegram" in block
    ]
    assert len(examples) == 1, "the README's Telegram example moved or was duplicated"
    send = examples[0]["actions"][0]["repeat"]["sequence"][0]
    assert send["action"] == "telegram_bot.send_message"
    return send["data"]["message"]


def _render(hass: HomeAssistant, payload: dict[str, Any]) -> str:
    """Render the documented Telegram template against one event payload."""
    return Template(_readme_telegram_message(), hass).async_render(
        {"trigger": {"event": {"data": payload}}}, parse_result=False
    )


async def test_the_documented_telegram_message_carries_the_item_preview(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The README's Telegram template renders the feed, the item and its summary.

    The preview is the point of the notification - it is what tells you whether
    the article is worth opening - so the whole message is pinned, blank line
    included, rather than only checking that the description appears somewhere.
    """
    entry = make_config_entry()
    seed_store(hass_storage, entry.entry_id, ["already-seen"])
    serve_keys(aioclient_mock, ["post-1"])
    events = async_capture_events(hass, EVENT_NEW_ITEM)

    await setup_entry(hass, entry)

    assert _render(hass, events[0].data) == (
        f"📰 {FEED_TITLE}\n"
        '<b><a href="https://example.com/posts/post-1">Post post-1</a></b>\n'
        "\n"
        "Body of post-1"
    )


# a feed that publishes whole articles rather than teasers; the cut has to land
# on a word boundary, so the tail is what must be gone
LONG_SUMMARY = " ".join(f"sentence-{index}" for index in range(120))


@pytest.mark.parametrize(
    ("summary_plain", "expected", "unexpected"),
    [
        # an item without any description must not trail off into blank lines
        ("", "</b>", "\n\n"),
        # publisher text carrying markup characters has to arrive escaped, or
        # Telegram rejects the message whole
        ("Cost 5 < 6 & rising", "Cost 5 &lt; 6 &amp; rising", "5 < 6 &"),
        (LONG_SUMMARY, "…", LONG_SUMMARY[-40:]),
    ],
    ids=["no description", "markup characters", "the whole article"],
)
async def test_the_documented_telegram_message_survives_awkward_descriptions(
    hass: HomeAssistant,
    summary_plain: str,
    expected: str,
    unexpected: str,
) -> None:
    """The documented template copes with an empty, a hostile and a huge summary."""
    message = _render(
        hass,
        {
            ATTR_FEED_TITLE: FEED_TITLE,
            ATTR_TITLE: "Post post-1",
            ATTR_LINK: "https://example.com/posts/post-1",
            ATTR_SUMMARY_PLAIN: summary_plain,
        },
    )

    assert message.endswith(expected)
    assert unexpected not in message
    # Telegram refuses a message over 4096 characters outright
    assert len(message) < 1000
