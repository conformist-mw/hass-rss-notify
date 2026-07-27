"""Diagnostics for one configured feed."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import RssNotifyConfigEntry
from .redact import redact_url, redact_urls


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RssNotifyConfigEntry
) -> dict[str, Any]:
    """Return the diagnostics of one feed.

    The seen-set is reported by size only: the keys themselves are item GUIDs
    and links, which would bloat the report without helping to debug it.
    """
    coordinator = entry.runtime_data
    store = coordinator.store
    # the download endpoint does not require a loaded entry, and an entry whose
    # first poll failed is exactly the one a user downloads diagnostics for: it
    # has a coordinator, but no poll outcome to report yet
    data = coordinator.data
    success = coordinator.last_update_success
    return {
        "options": dict(entry.options),
        "feed": {
            "url": redact_url(coordinator.url),
            # a feed reporting no title of its own is named after its host, but
            # a feed whose own <title> is a URL must not slip past redaction
            "title": redact_urls(coordinator.feed_title),
            "link": redact_url(coordinator.feed_link),
        },
        "state": {
            "seen_count": store.seen_count,
            # the validators are reported by presence only: their values say
            # nothing beyond whether the next poll can be answered with a 304
            "cache_validators": {
                "etag": store.etag is not None,
                "last_modified": store.last_modified is not None,
            },
            "last_batch_size": len(data.new_items) if data else None,
            "pending": data.pending if data else None,
        },
        "last_fetch": {
            "success": success,
            "time": coordinator.last_update_success_time,
            # `last_exception` is never cleared on success, so reporting it after
            # a recovered poll would pair `success: true` with an old error
            "error": None if success else redact_urls(str(coordinator.last_exception)),
        },
    }
