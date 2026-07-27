"""Masking of feed URLs for anything that leaves the instance.

A feed URL commonly carries basic-auth userinfo or an access token in its query
string, so no URL is handed out verbatim - not in a diagnostics report, and not
inside an error message that ends up in the log or as the entry's failure reason
in the UI.

The path is *not* masked: it is what identifies the feed in a report, and a
secret placed there (`/feed/<token>`) cannot be told apart from a normal path.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from homeassistant.components.diagnostics import REDACTED

# any scheme, not just http(s): an error message may quote the URL as the
# transport saw it
_URL_RE: Final = re.compile(r"""[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s'"<>]+""")


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
