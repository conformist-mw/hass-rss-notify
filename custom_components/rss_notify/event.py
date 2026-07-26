"""Event entity publishing the new items of one feed."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RssNotifyConfigEntry
from .const import (
    ATTR_MAX_LENGTH,
    ATTR_SUMMARY,
    ATTR_SUMMARY_PLAIN,
    DOMAIN,
    EVENT_TYPE_NEW_ITEM,
)
from .coordinator import RssFeedCoordinator, signal_new_item

# the coordinator centralizes all updates, so entities never poll
PARALLEL_UPDATES = 0

# item text is capped in the entity attributes so the recorder does not keep a
# copy of every article; the bus event still carries the full text
TRUNCATED_ATTRS: Final = (ATTR_SUMMARY, ATTR_SUMMARY_PLAIN)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RssNotifyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the event entity of one feed."""
    async_add_entities([RssFeedEventEntity(entry.runtime_data)])


class RssFeedEventEntity(CoordinatorEntity[RssFeedCoordinator], EventEntity):
    """One entity per feed, firing `new_item` for every new item.

    The entity is named after its feed (the device carries the feed title) and
    inherits its availability from `CoordinatorEntity`, so a failing poll makes
    it unavailable until the feed can be fetched again.
    """

    _attr_has_entity_name = True
    _attr_translation_key = EVENT_TYPE_NEW_ITEM

    def __init__(self, coordinator: RssFeedCoordinator) -> None:
        """Initialize the event entity from the coordinator of its feed."""
        super().__init__(coordinator)
        self._attr_event_types = [EVENT_TYPE_NEW_ITEM]
        self._entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{self._entry_id}_{EVENT_TYPE_NEW_ITEM}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, self._entry_id)})

    async def async_added_to_hass(self) -> None:
        """Subscribe to the items published by the coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_new_item(self._entry_id), self._async_handle_item
            )
        )

    @callback
    def _async_handle_item(self, payload: dict[str, Any]) -> None:
        """Trigger the entity event for a single new item."""
        self._trigger_event(EVENT_TYPE_NEW_ITEM, _event_attributes(payload))
        # `_trigger_event` does not write state, and a batch has to produce one
        # state change per item, so the write happens here per item
        self.async_write_ha_state()


def _event_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the event payload with its long text fields truncated."""
    attributes = dict(payload)
    for key in TRUNCATED_ATTRS:
        attributes[key] = attributes[key][:ATTR_MAX_LENGTH]
    return attributes
