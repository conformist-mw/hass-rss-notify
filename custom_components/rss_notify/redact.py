"""Masking of feed URLs for anything that leaves the instance.

A feed URL commonly carries basic-auth userinfo or an access token in its query
string, so no URL is handed out verbatim - not in a diagnostics report, not in a
log line (error or debug), and not inside an error message that ends up as the
entry's failure reason in the UI.

The path is *not* masked: it is what identifies the feed in a report, and a
secret placed there (`/feed/<token>`) cannot be told apart from a normal path.

The query is masked whole, keys included. A token is as often a bare key
(`?s3cret`) or a key named after itself as it is a value, and no rule can tell
an authenticating parameter from a formatting one.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit, urlunsplit

# the canonical constant: `homeassistant.components.diagnostics` states the same
# value, but importing the component would drag `http` and `websocket_api` into
# the feed client and the config flow, which this module also serves
from homeassistant.helpers.redact import REDACTED

# Any scheme, not just http(s): an error message may quote the URL as the
# transport saw it. Two shapes are matched, and the second one is deliberately
# narrow. `scheme://host/...` is unambiguous. An *opaque* URL (`mailto:a@b`,
# `data:...#x`) is only recognized when its opaque part carries an `@`, `?` or
# `#`, because a bare `scheme:word` pattern would also swallow the `ssl:default`
# and `example.com:443` that aiohttp puts in its connection errors - masking the
# host and port of a failure is a worse outcome than leaving `urn:x` alone.
_URL_RE: Final = re.compile(
    r"""[a-zA-Z][a-zA-Z0-9+.\-]*:(?://[^\s'"<>]+|[^\s'"<>]*[@?\#][^\s'"<>]*)"""
)


def redact_url(url: str) -> str:
    """Return `url` with userinfo and the whole query masked, minus its fragment.

    A URL with no host to keep - an opaque one such as `mailto:` or `urn:`, or
    one that cannot be split at all - is replaced whole: there is no field to
    mask in it, so nothing about it can be reported safely.
    """
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
    return urlunsplit(
        (
            parts.scheme,
            f"{REDACTED}@{host}" if userinfo else host,
            parts.path,
            REDACTED if parts.query else "",
            "",
        )
    )


def redact_urls(text: str) -> str:
    """Return `text` with every URL it mentions redacted."""
    return _URL_RE.sub(lambda match: redact_url(match.group()), text)
