"""Tests for the feed client: fetch, conditional GET, parse and normalize."""

from datetime import datetime
import hashlib
from http import HTTPStatus
import logging
from typing import Any

import aiohttp
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rss_notify.client import (
    USER_AGENT,
    FeedFetchError,
    FeedParseError,
    FetchResult,
    NotModified,
    async_fetch_feed,
    async_parse_feed,
    sort_items_oldest_first,
    to_plain_text,
)

from .conftest import FEED_URL, load_feed

# a feed behind basic auth whose query string carries an access token
SECRET_URL = "https://feeduser:s3cret@example.com/private/rss?token=t0ken"
SECRET_PARTS = ("s3cret", "t0ken", "feeduser")

EMPTY_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel>'
    b"<title>Empty</title><link>https://empty.example.com/</link>"
    b"</channel></rss>"
)

ATOM_FEED = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<feed xmlns="http://www.w3.org/2005/Atom">'
    b"<title>Atom Blog</title>"
    b'<link href="https://atom.example.com/"/>'
    b"<entry><title>Atom post</title>"
    b'<link href="https://atom.example.com/posts/1"/>'
    b"<id>urn:uuid:atom-1</id>"
    b"<updated>2026-07-24T06:00:00Z</updated>"
    b'<content type="html">&lt;p&gt;Atom body&lt;/p&gt;</content>'
    b"</entry></feed>"
)

# The undeclared `&raquo;` entity makes feedparser flag the feed as bozo, but it
# still recovers and returns the entry.
BOZO_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel>'
    b"<title>Bozo</title><link>https://bozo.example.com/</link>"
    b"<item><title>Broken &raquo; but usable</title>"
    b"<guid>bozo-1</guid></item>"
    b"</channel></rss>"
)


async def test_fetch_and_parse_basic(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A normal feed is fetched, parsed and normalized in feed order."""
    aioclient_mock.get(
        FEED_URL,
        content=load_feed("feed_basic"),
        headers={"ETag": '"abc"', "Last-Modified": "Fri, 24 Jul 2026 12:00:00 GMT"},
    )

    result = await async_fetch_feed(hass, FEED_URL)

    assert isinstance(result, FetchResult)
    assert result.feed_title == "Example Blog"
    assert result.feed_link == "https://example.com/"
    assert result.etag == '"abc"'
    assert result.last_modified == "Fri, 24 Jul 2026 12:00:00 GMT"
    # feed order is preserved by the client (the coordinator does the sorting)
    assert [item.key for item in result.items] == ["post-3", "post-2", "post-1"]

    newest = result.items[0]
    assert newest.title == "Third post"
    assert newest.link == "https://example.com/posts/3"
    assert newest.summary == "<p>Third &amp; newest post</p>"
    assert newest.summary_plain == "Third & newest post"
    assert newest.published == datetime(2026, 7, 24, 12, 0, tzinfo=dt_util.UTC)

    # the user agent is fixed: some publishers filter on it, so a change to it
    # is a change in behaviour and has to be a deliberate one
    _method, _url, _data, headers = aioclient_mock.mock_calls[0]
    assert headers["User-Agent"] == "HomeAssistant-rss_notify"
    assert USER_AGENT == "HomeAssistant-rss_notify"
    assert "If-None-Match" not in headers
    assert "If-Modified-Since" not in headers


async def test_conditional_get_sends_validators(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Stored validators are sent as conditional GET headers."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))

    await async_fetch_feed(
        hass,
        FEED_URL,
        etag='"abc"',
        last_modified="Fri, 24 Jul 2026 12:00:00 GMT",
    )

    _method, _url, _data, headers = aioclient_mock.mock_calls[0]
    assert headers["If-None-Match"] == '"abc"'
    assert headers["If-Modified-Since"] == "Fri, 24 Jul 2026 12:00:00 GMT"


async def test_not_modified(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 304 response yields NotModified without touching the payload."""
    aioclient_mock.get(FEED_URL, status=HTTPStatus.NOT_MODIFIED)

    result = await async_fetch_feed(hass, FEED_URL, etag='"abc"')

    assert isinstance(result, NotModified)


async def test_key_falls_back_to_link(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Items without a guid are identified by their link."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_no_guid"))

    result = await async_fetch_feed(hass, FEED_URL)

    assert isinstance(result, FetchResult)
    assert [item.key for item in result.items] == [
        "https://noguid.example.com/posts/2",
        "https://noguid.example.com/posts/1",
    ]


async def test_key_falls_back_to_fingerprint_and_skips_unidentifiable(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Items without guid/link get a fingerprint key; empty items are skipped."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_no_ids"))

    with caplog.at_level(logging.WARNING):
        result = await async_fetch_feed(hass, FEED_URL)

    assert isinstance(result, FetchResult)
    expected = hashlib.sha256(
        "|".join(
            (
                "Fingerprinted post",
                "Fri, 24 Jul 2026 08:00:00 GMT",
                "Body used for the fingerprint",
            )
        ).encode()
    ).hexdigest()
    assert [item.key for item in result.items] == [expected]
    assert "without any usable identity" in caplog.text


async def test_sort_items_oldest_first(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Items sort oldest to newest, undated ones first and document order reversed.

    The fixture lists undated-a, dated-b, undated-c, dated-d. A document lists
    its newest item first, so undated-c (further down) is older than undated-a.
    """
    aioclient_mock.get(FEED_URL, content=load_feed("feed_no_dates"))

    result = await async_fetch_feed(hass, FEED_URL)

    assert isinstance(result, FetchResult)
    assert [item.key for item in sort_items_oldest_first(result.items)] == [
        "undated-c",
        "undated-a",
        "dated-d",
        "dated-b",
    ]


async def test_sort_of_an_entirely_undated_feed_reverses_the_document(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With no date anywhere, the topmost item of the document is the newest."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_undated"))

    result = await async_fetch_feed(hass, FEED_URL)

    assert isinstance(result, FetchResult)
    assert [item.key for item in result.items] == ["u-3", "u-2", "u-1"]
    assert [item.key for item in sort_items_oldest_first(result.items)] == [
        "u-1",
        "u-2",
        "u-3",
    ]


async def test_malformed_feed_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A document that is not a feed raises FeedParseError."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_malformed"))

    with pytest.raises(FeedParseError):
        await async_fetch_feed(hass, FEED_URL)


async def test_http_error_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An HTTP error status raises FeedFetchError."""
    aioclient_mock.get(FEED_URL, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    with pytest.raises(FeedFetchError):
        await async_fetch_feed(hass, FEED_URL)


async def test_network_error_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transport error raises FeedFetchError."""
    aioclient_mock.get(FEED_URL, exc=aiohttp.ClientError("boom"))

    with pytest.raises(FeedFetchError, match="Error fetching feed"):
        await async_fetch_feed(hass, FEED_URL)


async def test_timeout_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A timeout raises FeedFetchError."""
    aioclient_mock.get(FEED_URL, exc=TimeoutError())

    with pytest.raises(FeedFetchError, match="Timeout"):
        await async_fetch_feed(hass, FEED_URL, timeout_seconds=1)


def capture_request_kwargs(hass: HomeAssistant) -> dict[str, Any]:
    """Return a dict filled with the kwargs of the next request of the session."""
    session = async_get_clientsession(hass)
    captured: dict[str, Any] = {}
    original = session._request

    async def spy(method: str, url: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return await original(method, url, **kwargs)

    # the mocker patches the session the same way; plain setattr would warn
    object.__setattr__(session, "_request", spy)
    return captured


async def test_request_carries_the_fixed_timeout(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Every request is bounded by the fixed 30 second timeout."""
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))
    captured = capture_request_kwargs(hass)

    await async_fetch_feed(hass, FEED_URL)

    assert captured["timeout"].total == 30


async def test_oversized_body_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response bigger than the cap is rejected instead of being buffered."""
    monkeypatch.setattr("custom_components.rss_notify.client.MAX_FEED_BYTES", 64)
    aioclient_mock.get(FEED_URL, content=load_feed("feed_basic"))

    with pytest.raises(FeedFetchError, match="larger than the 64 byte limit"):
        await async_fetch_feed(hass, FEED_URL)


@pytest.mark.parametrize(
    ("mock_kwargs", "match"),
    [
        ({"status": HTTPStatus.INTERNAL_SERVER_ERROR}, "Error fetching feed"),
        ({"exc": TimeoutError()}, "Timeout fetching feed"),
        ({"content": b"<html><body>not a feed</body></html>"}, "Unable to parse feed"),
    ],
)
async def test_errors_never_quote_the_feed_credentials(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_kwargs: dict[str, Any],
    match: str,
) -> None:
    """Error messages carry a redacted URL: they are logged and shown in the UI."""
    aioclient_mock.get(SECRET_URL, **mock_kwargs)

    with pytest.raises((FeedFetchError, FeedParseError), match=match) as raised:
        await async_fetch_feed(hass, SECRET_URL)

    message = str(raised.value)
    assert REDACTED in message
    assert not any(secret in message for secret in SECRET_PARTS)


async def test_atom_feed_uses_id_content_and_updated(hass: HomeAssistant) -> None:
    """An Atom entry is keyed by its id and dated from `updated`."""
    result = await async_parse_feed(hass, ATOM_FEED, FEED_URL)

    assert result.feed_title == "Atom Blog"
    assert result.feed_link == "https://atom.example.com/"
    item = result.items[0]
    assert item.key == "urn:uuid:atom-1"
    assert item.link == "https://atom.example.com/posts/1"
    assert item.summary == "<p>Atom body</p>"
    assert item.summary_plain == "Atom body"
    assert item.published == datetime(2026, 7, 24, 6, 0, tzinfo=dt_util.UTC)


async def test_empty_but_valid_feed_has_no_items(hass: HomeAssistant) -> None:
    """A valid feed with no items parses into an empty item list."""
    result = await async_parse_feed(hass, EMPTY_FEED, FEED_URL)

    assert result.items == []
    assert result.feed_title == "Empty"


async def test_feed_meta_carries_no_surrounding_whitespace(hass: HomeAssistant) -> None:
    """The feed's own title and link arrive without surrounding whitespace.

    A padded title would end up as the device and entity name of the feed. The
    pinned feedparser already strips text nodes, so this pins the contract rather
    than one implementation of it - `client.py` strips again for the same reason.
    """
    payload = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<title>  Padded Blog\n</title><link> https://padded.example.com/ </link>"
        b"<item><title>Post</title><guid>padded-1</guid></item>"
        b"</channel></rss>"
    )

    result = await async_parse_feed(hass, payload, FEED_URL)

    assert result.feed_title == "Padded Blog"
    assert result.feed_link == "https://padded.example.com/"


async def test_recoverable_parse_problem_logs_warning(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A bozo feed that still yields entries is used, with a warning logged."""
    with caplog.at_level(logging.WARNING):
        result = await async_parse_feed(hass, BOZO_FEED, FEED_URL)

    assert [item.key for item in result.items] == ["bozo-1"]
    assert "Possible issue parsing feed" in caplog.text


async def test_a_lasting_feed_problem_is_warned_about_only_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A defect that does not heal warns once, then stays at debug level.

    The same feed is polled every few minutes and the defect is still there
    every time, so a plain warning would add thousands of log lines a day.
    """
    with caplog.at_level(logging.DEBUG):
        await async_parse_feed(hass, BOZO_FEED, FEED_URL)
        await async_parse_feed(hass, BOZO_FEED, FEED_URL)
        await async_parse_feed(hass, BOZO_FEED, FEED_URL)

    levels = [
        record.levelno
        for record in caplog.records
        if "Possible issue parsing feed" in record.getMessage()
    ]
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]


def test_to_plain_text() -> None:
    """HTML is reduced to collapsed, unescaped plain text."""
    assert to_plain_text("") == ""
    assert (
        to_plain_text("<p>Hello&nbsp;&amp; <b>welcome</b></p>\n<p>Bye</p>")
        == "Hello & welcome Bye"
    )
