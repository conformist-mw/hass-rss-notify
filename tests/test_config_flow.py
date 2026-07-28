"""Tests for the RSS Notify config and options flows."""

from collections.abc import Generator
from datetime import timedelta
from http import HTTPStatus
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import homeassistant.helpers.config_validation as cv
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
import voluptuous as vol
import voluptuous_serialize

from custom_components.rss_notify import config_flow
from custom_components.rss_notify.client import NotModified
from custom_components.rss_notify.config_flow import _fallback_name
from custom_components.rss_notify.const import (
    CONF_INITIAL_ITEMS,
    CONF_MAX_ITEMS_PER_POLL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

from .conftest import (
    DEFAULT_OPTIONS,
    FEED_URL,
    SECRET_PARTS,
    SECRET_URL,
    load_feed,
    serve_keys,
    setup_entry,
)

UNTITLED_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel>'
    b"<item><title>Only post</title><guid>only-1</guid></item>"
    b"</channel></rss>"
)


@pytest.fixture
def patch_setup() -> bool:
    """Whether entry setup is mocked away; parametrize to False for a real setup."""
    return True


@pytest.fixture(autouse=True)
def mock_setup_entry(patch_setup: bool) -> Generator[AsyncMock | None]:
    """Prevent the created entry from being set up for real."""
    if not patch_setup:
        yield None
        return
    with patch(
        "custom_components.rss_notify.async_setup_entry", return_value=True
    ) as mock:
        yield mock


def suggested_values(schema: vol.Schema) -> dict[str, Any]:
    """Return the values a form pre-fills, keyed by field name."""
    return {
        str(key): key.description["suggested_value"]
        for key in schema.schema
        if isinstance(key.description, dict)
    }


async def _start_flow(hass: HomeAssistant) -> str:
    """Start the user flow and return its id, asserting the URL form is shown."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]
    # the URL step asks for nothing else: the name to propose is the feed's own
    # title, which is unknown until the feed has been fetched
    assert list(result["data_schema"].schema) == [CONF_URL]
    return result["flow_id"]


async def _submit_url(
    hass: HomeAssistant, flow_id: str, url: str = FEED_URL
) -> dict[str, Any]:
    """Submit `url` and return the name form it leads to."""
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_URL: url})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name"
    return result


async def test_user_flow_creates_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_setup_entry: AsyncMock,
) -> None:
    """The name step offers the feed's own title, and accepting it creates the entry."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))
    flow_id = await _start_flow(hass)

    form = await _submit_url(hass, flow_id, f"  {FEED_URL}  ")

    # this is the point of the second step: the name arrives filled in, and the
    # polling options along with it, so accepting the form is a complete answer
    assert suggested_values(form["data_schema"]) == {
        CONF_NAME: "Example Blog",
        **DEFAULT_OPTIONS,
    }
    assert form["description_placeholders"] == {"item_count": "3"}

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Example Blog"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Example Blog"
    # the name lives in the entry title alone, so a UI rename cannot desync it
    assert result["data"] == {CONF_URL: FEED_URL}
    assert result["options"] == DEFAULT_OPTIONS
    assert result["result"].unique_id == FEED_URL
    assert len(mock_setup_entry.mock_calls) == 1


async def test_user_flow_custom_name_wins(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A name the user edits into the field overrides the feed title."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))
    flow_id = await _start_flow(hass)
    await _submit_url(hass, flow_id)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "  My feed  "}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My feed"


@pytest.mark.parametrize("submitted", [{}, {CONF_NAME: "   "}])
async def test_a_cleared_name_keeps_the_suggestion(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    submitted: dict[str, str],
) -> None:
    """Clearing the pre-filled name falls back to it instead of failing the form.

    The field is optional for exactly this reason: an entry title may not be
    empty, and a required field would answer an emptied one with a form error.
    """
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))
    flow_id = await _start_flow(hass)
    await _submit_url(hass, flow_id)

    result = await hass.config_entries.flow.async_configure(flow_id, submitted)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Example Blog"


async def test_the_add_flow_stores_the_polling_options(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Options set while adding the feed are the ones the entry is created with."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))
    flow_id = await _start_flow(hass)
    await _submit_url(hass, flow_id)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Example Blog",
            CONF_UPDATE_INTERVAL: 15,
            CONF_INITIAL_ITEMS: 0,
            CONF_MAX_ITEMS_PER_POLL: 0,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # a number selector yields floats; whole numbers are stored
    assert result["options"] == {
        CONF_UPDATE_INTERVAL: 15,
        CONF_INITIAL_ITEMS: 0,
        CONF_MAX_ITEMS_PER_POLL: 0,
    }
    assert all(isinstance(value, int) for value in result["options"].values())


async def test_a_cleared_option_falls_back_to_its_default(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An option field left empty is created with the default, not left unset.

    The fields are optional so that clearing one is not a form error, which means
    a cleared field arrives as a missing key rather than as a value.
    """
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))
    flow_id = await _start_flow(hass)
    await _submit_url(hass, flow_id)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_UPDATE_INTERVAL: 15}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {**DEFAULT_OPTIONS, CONF_UPDATE_INTERVAL: 15}


async def test_the_add_flow_rejects_a_fractional_option(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A fraction fails the name step instead of creating a feed with a truncated cap.

    `max_items_per_poll: 0.9` truncated is `0`, which means *unlimited*. The form
    comes back with the error on that field and keeps the rest of what was typed,
    the name included.
    """
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))
    flow_id = await _start_flow(hass)
    await _submit_url(hass, flow_id)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "My feed", CONF_MAX_ITEMS_PER_POLL: 0.9}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name"
    assert result["errors"] == {CONF_MAX_ITEMS_PER_POLL: "not_a_whole_number"}
    assert suggested_values(result["data_schema"]) == {
        **DEFAULT_OPTIONS,
        CONF_NAME: "My feed",
        CONF_MAX_ITEMS_PER_POLL: 0.9,
    }
    assert not hass.config_entries.async_entries(DOMAIN)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "My feed", CONF_MAX_ITEMS_PER_POLL: 1}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {**DEFAULT_OPTIONS, CONF_MAX_ITEMS_PER_POLL: 1}


@pytest.mark.parametrize(
    ("url", "expected_title"),
    [
        (FEED_URL, "example.com"),
        # the name becomes the entry title, the device name, the entity name and
        # the feed_title of every event: none of those may carry a credential
        (
            "https://feeduser:s3cret@private.example.com/rss?token=t0ken",
            "private.example.com",
        ),
    ],
)
async def test_user_flow_falls_back_to_the_feed_host_as_title(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    url: str,
    expected_title: str,
) -> None:
    """A feed reporting no title is proposed under its host, not under its URL."""
    aioclient_mock.get(url, content=UNTITLED_FEED)
    flow_id = await _start_flow(hass)

    form = await _submit_url(hass, flow_id, url)

    assert suggested_values(form["data_schema"])[CONF_NAME] == expected_title

    result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == expected_title


def test_the_host_fallback_always_yields_a_name() -> None:
    """A URL without a host still produces a usable entry title.

    The validation fetch rejects a hostless URL long before the name is built, so
    the flow cannot reach this - but a config entry may not carry an empty title,
    and `urlsplit().hostname` is `None` for such a URL.
    """
    assert _fallback_name("https:///feed.xml") == "RSS feed"


async def test_duplicate_feed_aborts(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Adding an already configured feed URL aborts without fetching it."""
    mock_config_entry.add_to_hass(hass)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_URL: FEED_URL}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert not aioclient_mock.mock_calls


@pytest.mark.parametrize(
    ("mock_kwargs", "expected_error"),
    [
        ({"exc": aiohttp.ClientError("boom")}, "cannot_connect"),
        ({"status": HTTPStatus.INTERNAL_SERVER_ERROR}, "cannot_connect"),
        ({"exc": TimeoutError()}, "cannot_connect"),
        ({"content": b"<html><body>not a feed</body></html>"}, "invalid_feed"),
    ],
)
async def test_flow_errors_recover(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_kwargs: dict[str, object],
    expected_error: str,
) -> None:
    """A failing feed re-shows the form with an error and recovers afterwards."""
    aioclient_mock.get(FEED_URL, **mock_kwargs)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_URL: FEED_URL}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected_error}

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))

    await _submit_url(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Example Blog"


@pytest.mark.parametrize(
    "url",
    [
        # what a site embeds in <link href>, so a pasteable mistake. aiohttp's
        # connector answers it with a bare AssertionError, which is neither a
        # ClientError nor a TimeoutError, so it used to leave the flow as an
        # unknown error with a logged traceback instead of a form error.
        "//feeduser:s3cret@example.com/rss",
        "//example.com/rss",
        # the URL selector performs no server-side validation, so any string the
        # user types arrives here
        "mailto:s3cret@example.com",
        "urn:s3cret",
        "ftp://example.com/feed",
        "http://[::1",
    ],
)
async def test_a_malformed_url_is_a_form_error_not_a_crash(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    url: str,
) -> None:
    """A URL that is not http(s) re-shows the form instead of escaping the flow."""
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_URL: url})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert not aioclient_mock.mock_calls
    assert "s3cret" not in caplog.text


async def test_the_name_step_quotes_no_part_of_the_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Nothing the name step renders carries a part of the feed URL.

    The step's description and its pre-filled name are shown in the UI, and a
    feed URL commonly carries basic-auth userinfo or an access token. Only the
    item count is passed through as a placeholder, and the name falls back to the
    host alone.
    """
    aioclient_mock.get(SECRET_URL, content=UNTITLED_FEED)
    flow_id = await _start_flow(hass)

    form = await _submit_url(hass, flow_id, SECRET_URL)

    assert form["description_placeholders"] == {"item_count": "1"}
    rendered = "".join(
        str(part)
        for part in (
            form["description_placeholders"],
            suggested_values(form["data_schema"]),
        )
    )
    assert not any(part in rendered for part in SECRET_PARTS)


async def test_unexpected_not_modified_is_invalid_feed(hass: HomeAssistant) -> None:
    """A 'not modified' answer to the unconditional validation fetch is an error."""
    flow_id = await _start_flow(hass)

    with patch(
        "custom_components.rss_notify.config_flow.async_fetch_feed",
        return_value=NotModified(),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_URL: FEED_URL}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_feed"}


async def test_options_flow_saves_new_values(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The options form offers the values in use and stores the submitted ones."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert suggested_values(result["data_schema"]) == DEFAULT_OPTIONS

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_UPDATE_INTERVAL: 15,
            CONF_INITIAL_ITEMS: 0,
            CONF_MAX_ITEMS_PER_POLL: 0,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # a number selector yields floats; whole numbers are stored
    assert result["data"] == {
        CONF_UPDATE_INTERVAL: 15,
        CONF_INITIAL_ITEMS: 0,
        CONF_MAX_ITEMS_PER_POLL: 0,
    }
    assert all(isinstance(value, int) for value in result["data"].values())
    assert dict(mock_config_entry.options) == result["data"]


@pytest.mark.parametrize(
    "invalid_option",
    [
        {CONF_UPDATE_INTERVAL: 0},
        {CONF_INITIAL_ITEMS: -1},
        {CONF_MAX_ITEMS_PER_POLL: -1},
    ],
)
async def test_options_flow_rejects_values_below_the_minimum(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    invalid_option: dict[str, int],
) -> None:
    """Values under the field minimum are refused and change nothing."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**DEFAULT_OPTIONS, **invalid_option}
        )

    assert dict(mock_config_entry.options) == DEFAULT_OPTIONS


@pytest.mark.parametrize(
    "fractional_option",
    [
        # 0.9 is the one that bites: truncating it yields 0, and 0 means
        # *unlimited*, the opposite of what such a value asks for
        {CONF_MAX_ITEMS_PER_POLL: 0.9},
        {CONF_INITIAL_ITEMS: 1.5},
        {CONF_UPDATE_INTERVAL: 5.5},
    ],
)
async def test_options_flow_rejects_fractional_values(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fractional_option: dict[str, float],
) -> None:
    """A fraction comes back as a form error rather than being truncated.

    A number selector does not enforce its own `step`, so nothing but this stops
    a fraction reaching the coercion that would round it away. The error is
    reported on the offending field and the form keeps what was typed.
    """
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**DEFAULT_OPTIONS, **fractional_option}
    )

    field = next(iter(fractional_option))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: "not_a_whole_number"}
    assert suggested_values(result["data_schema"]) == {
        **DEFAULT_OPTIONS,
        **fractional_option,
    }
    assert dict(mock_config_entry.options) == DEFAULT_OPTIONS


@pytest.mark.parametrize("patch_setup", [False])
async def test_options_flow_reload_applies_the_new_options(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Saving options reloads the feed, so its coordinator polls with them."""
    serve_keys(aioclient_mock, ["post-1", "post-2"])
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.update_interval == timedelta(minutes=DEFAULT_UPDATE_INTERVAL)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_UPDATE_INTERVAL: 15,
            CONF_INITIAL_ITEMS: 3,
            CONF_MAX_ITEMS_PER_POLL: 0,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    reloaded = mock_config_entry.runtime_data
    assert reloaded is not coordinator
    assert reloaded.update_interval == timedelta(minutes=15)
    assert reloaded.initial_items == 3
    assert reloaded.max_items_per_poll == 0


def _form_schemas() -> list[Any]:
    """Return every schema this flow module shows in the UI."""
    return [
        pytest.param(schema, id=name)
        for name, schema in vars(config_flow).items()
        if name.endswith("_SCHEMA")
    ]


@pytest.mark.parametrize("schema", _form_schemas())
def test_every_form_schema_is_serializable(schema: vol.Schema) -> None:
    """A schema the frontend asks for must survive `voluptuous_serialize`.

    This is what `helpers/data_entry_flow.py` does with the `data_schema` of
    every form; a schema it cannot convert answers the flow endpoint with a 500
    and the dialog never opens. Selectors convert, validator functions do not,
    so no `vol.All(selector, callable)` may reach a form. Driving a flow through
    `async_init` / `async_configure` proves nothing here: that path hands the
    schema straight back to the caller and never serializes it.
    """
    voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)
