"""Fixtures for the RSS Notify tests."""

from collections.abc import Generator

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    load_fixture_bytes,
)

from custom_components.rss_notify.const import (
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

FEED_URL = "https://example.com/rss"


def load_feed(name: str) -> bytes:
    """Return the raw bytes of a feed fixture from `tests/fixtures`."""
    return load_fixture_bytes(f"{name}.xml")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading of custom integrations in all tests."""
    yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for a single feed with default options."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Example Blog",
        unique_id=FEED_URL,
        data={CONF_URL: FEED_URL, CONF_NAME: "Example Blog"},
        options={
            CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
            CONF_INITIAL_ITEMS: DEFAULT_INITIAL_ITEMS,
            CONF_MAX_ITEMS_PER_POLL: DEFAULT_MAX_ITEMS_PER_POLL,
        },
    )
