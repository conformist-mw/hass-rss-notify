"""Config flow for the RSS Notify integration."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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


def _fallback_name(url: str) -> str:
    """Return the name of a feed that reports no title and was given none.

    Only the host of the URL is used. The name becomes the entry title, the
    device name, the entity name and the `feed_title` of every event, none of
    which are ever redacted - while a feed URL commonly carries basic-auth
    userinfo or an access token.
    """
    return urlsplit(url).hostname or "RSS feed"


def _count_field(minimum: int) -> vol.All:
    """Return a whole-number field with `minimum` enforced by the schema.

    A number selector hands back a float, so the value is coerced to `int`:
    both counts are used as list indices and the interval as whole minutes.
    """
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(min=minimum, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Coerce(int),
    )


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_UPDATE_INTERVAL): _count_field(1),
        vol.Required(CONF_INITIAL_ITEMS): _count_field(0),
        vol.Required(CONF_MAX_ITEMS_PER_POLL): _count_field(0),
    }
)


class RssNotifyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RSS Notify."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RssNotifyOptionsFlow:
        """Return the options flow handling the per-feed options."""
        return RssNotifyOptionsFlow()

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
                name = (
                    (user_input.get(CONF_NAME) or "").strip()
                    or feed_title
                    or _fallback_name(url)
                )
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


class RssNotifyOptionsFlow(OptionsFlow):
    """Handle the polling options of one configured feed."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form, pre-filled with the values in use."""
        if user_input is not None:
            # the entry's update listener reloads the feed, which rebuilds the
            # coordinator from the new options
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
