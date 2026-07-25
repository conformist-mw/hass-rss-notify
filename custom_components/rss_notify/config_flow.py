"""Config flow for the RSS Notify integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .client import FeedFetchError, FeedParseError, NotModified, async_fetch_feed
from .const import (
    CONF_INITIAL_ITEMS,
    CONF_MAX_ITEMS_PER_POLL,
    CONF_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
    DEFAULT_INITIAL_ITEMS,
    DEFAULT_MAX_ITEMS_PER_POLL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Optional(CONF_NAME): TextSelector(),
    }
)


class RssNotifyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RSS Notify."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add one feed, validating it by fetching and parsing it once."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_URL].strip()
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()

            try:
                feed_title = await self._async_validate_feed(url)
            except FeedFetchError as err:
                _LOGGER.debug("Cannot connect to feed %s: %s", url, err)
                errors["base"] = "cannot_connect"
            except FeedParseError as err:
                _LOGGER.debug("Feed %s is not usable: %s", url, err)
                errors["base"] = "invalid_feed"
            else:
                name = (user_input.get(CONF_NAME) or "").strip() or feed_title or url
                return self.async_create_entry(
                    title=name,
                    data={CONF_URL: url, CONF_NAME: name},
                    options={
                        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
                        CONF_INITIAL_ITEMS: DEFAULT_INITIAL_ITEMS,
                        CONF_MAX_ITEMS_PER_POLL: DEFAULT_MAX_ITEMS_PER_POLL,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def _async_validate_feed(self, url: str) -> str:
        """Return the title of the feed at `url`, raising when it is unusable."""
        result = await async_fetch_feed(async_get_clientsession(self.hass), url)
        if isinstance(result, NotModified):
            # unreachable in practice: the flow never sends cache validators
            raise FeedParseError(f"Unexpected 'not modified' response from {url}")
        return result.feed_title
