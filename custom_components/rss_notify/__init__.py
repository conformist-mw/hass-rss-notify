"""The RSS Notify integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_URL

type RssNotifyConfigEntry = ConfigEntry[RssNotifyRuntimeData]


@dataclass
class RssNotifyRuntimeData:
    """Runtime data stored on a RSS Notify config entry."""

    url: str


async def async_setup_entry(hass: HomeAssistant, entry: RssNotifyConfigEntry) -> bool:
    """Set up RSS Notify from a config entry."""
    entry.runtime_data = RssNotifyRuntimeData(url=entry.data[CONF_URL])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RssNotifyConfigEntry) -> bool:
    """Unload a config entry."""
    return True
