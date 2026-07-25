"""Config flow for the RSS Notify integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class RssNotifyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RSS Notify."""

    VERSION = 1
