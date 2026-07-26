"""Diagnostics for one configured feed."""

from __future__ import annotations

import re
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant

from . import RssNotifyConfigEntry

# A feed URL may carry basic-auth userinfo or a token in its query string, so no
# URL is ever handed out verbatim - not even inside an error message quoting it.
_URL_RE: Final = re.compile(r"""https?://[^\s'"<>]+""")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RssNotifyConfigEntry
) -> dict[str, Any]:
    """Return the diagnostics of one feed.

    The seen-set is reported by size only: the keys themselves are item GUIDs
    and links, which would bloat the report without helping to debug it.
    """
    coordinator = entry.runtime_data
    # the download endpoint does not require a loaded entry, and an entry whose
    # first poll failed is exactly the one a user downloads diagnostics for: it
    # has a coordinator, but no poll outcome to report yet
    data = coordinator.data
    error = coordinator.last_exception
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
            "seen_count": coordinator.seen_count,
            "cache_validators": coordinator.cache_validators,
            "last_batch_size": len(data.new_items) if data else None,
            "pending": data.pending if data else None,
        },
        "last_fetch": {
            "success": coordinator.last_update_success,
            "time": coordinator.last_update_success_time,
            "error": redact_urls(str(error)) if error else None,
        },
    }


def redact_url(url: str) -> str:
    """Return `url` with userinfo, query values and fragment masked."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        # not something that can be masked field by field, so mask it whole
        return REDACTED
    if not parts.netloc:
        return REDACTED

    userinfo, _, host = parts.netloc.rpartition("@")
    query = "&".join(
        f"{key}={REDACTED}" for key, _ in parse_qsl(parts.query, keep_blank_values=True)
    )
    return urlunsplit(
        (
            parts.scheme,
            f"{REDACTED}@{host}" if userinfo else host,
            parts.path,
            query,
            "",
        )
    )


def redact_urls(text: str) -> str:
    """Return `text` with every URL it mentions redacted."""
    return _URL_RE.sub(lambda match: redact_url(match.group()), text)
