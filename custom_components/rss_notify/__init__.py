"""The RSS Notify integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import RssFeedCoordinator
from .storage import SeenStore

PLATFORMS: list[Platform] = [Platform.EVENT]

type RssNotifyConfigEntry = ConfigEntry[RssFeedCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: RssNotifyConfigEntry) -> bool:
    """Set up one feed from a config entry."""
    store = SeenStore(hass, entry.entry_id)
    await store.async_load()

    coordinator = RssFeedCoordinator(hass, entry, store)
    entry.runtime_data = coordinator
    _async_register_device(hass, entry, coordinator)

    # the event entity has to exist before the first poll, otherwise the items
    # of the initial sync would be published into the void
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # HA does not unload an already forwarded platform when the setup of an
        # entry fails, so the retry would trip over "has already been setup" and
        # end up with an entity bound to the coordinator of this failed attempt
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        raise

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RssNotifyConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the persisted state of a feed that is being removed."""
    await SeenStore(hass, entry.entry_id).async_remove()


async def _async_options_updated(
    hass: HomeAssistant, entry: RssNotifyConfigEntry
) -> None:
    """Reload the entry so changed options take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_register_device(
    hass: HomeAssistant, entry: RssNotifyConfigEntry, coordinator: RssFeedCoordinator
) -> None:
    """Register the device grouping the entities of one feed."""
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        entry_type=dr.DeviceEntryType.SERVICE,
        name=coordinator.feed_title,
        configuration_url=coordinator.url,
    )
