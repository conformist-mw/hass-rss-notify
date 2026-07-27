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
from homeassistant.const import CONF_NAME, CONF_URL
from homeassistant.core import callback
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
    CONF_UPDATE_INTERVAL,
    DEFAULT_INITIAL_ITEMS,
    DEFAULT_MAX_ITEMS_PER_POLL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .redact import redact_url

_LOGGER = logging.getLogger(__name__)


def _fallback_name(url: str) -> str:
    """Return the name of a feed that reports no title and was given none.

    Only the host of the URL is used. The name becomes the entry title, the
    device name, the entity name and the `feed_title` of every event, none of
    which are ever redacted - while a feed URL commonly carries basic-auth
    userinfo or an access token. A hostless URL never gets this far (the
    validation fetch rejects it), but an entry title may not be empty either.
    """
    return urlsplit(url).hostname or "RSS feed"


def _whole_number(value: float) -> int:
    """Return `value` as an `int`, rejecting anything with a fractional part.

    A number selector hands back a float and does not enforce its own `step`, so
    a fraction would otherwise be truncated in silence - and truncation is not a
    harmless rounding here: `max_items_per_poll: 0.9` would become `0`, which
    means *unlimited*, the opposite of what such a value asks for.
    """
    number = float(value)
    if not number.is_integer():
        raise vol.Invalid("expected a whole number")
    return int(number)


def _whole_number_field(minimum: int) -> vol.All:
    """Return a whole-number field with `minimum` enforced by the schema."""
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(min=minimum, step=1, mode=NumberSelectorMode.BOX)
        ),
        _whole_number,
    )


# `CONF_NAME` is a form field only: the name it collects is stored as the entry
# title, never in `entry.data`
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Optional(CONF_NAME): TextSelector(),
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_UPDATE_INTERVAL): _whole_number_field(1),
        vol.Required(CONF_INITIAL_ITEMS): _whole_number_field(0),
        vol.Required(CONF_MAX_ITEMS_PER_POLL): _whole_number_field(0),
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
                _LOGGER.debug("Cannot connect to feed %s: %s", redact_url(url), err)
                errors["base"] = "cannot_connect"
            except FeedParseError as err:
                _LOGGER.debug("Feed %s is not usable: %s", redact_url(url), err)
                errors["base"] = "invalid_feed"
            else:
                name = (
                    (user_input.get(CONF_NAME) or "").strip()
                    or feed_title
                    or _fallback_name(url)
                )
                # the name lives in the entry title only: HA keeps that in step
                # with a rename in the UI, and the coordinator reads it from
                # there, so the two can never drift apart
                return self.async_create_entry(
                    title=name,
                    data={CONF_URL: url},
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
        result = await async_fetch_feed(self.hass, url)
        if isinstance(result, NotModified):
            # unreachable in practice: the flow never sends cache validators
            raise FeedParseError(
                f"Unexpected 'not modified' response from {redact_url(url)}"
            )
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
