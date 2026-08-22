"""Tests for the feed client: fetch, conditional GET, parse and normalize."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
from http import HTTPStatus
import logging
from pathlib import Path
import traceback
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.redact import REDACTED
from homeassistant.util import dt as dt_util
from multidict import CIMultiDict, CIMultiDictProxy
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from yarl import URL

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

from .conftest import FEED_LINK, FEED_URL, SECRET_PARTS, SECRET_URL, load_feed

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

# a full-content feed of a routine size for a WordPress or Atom blog: over a
# megabyte, and far more than one buffered socket read
LARGE_FEED_ITEMS = 2000


def large_feed(items: int = LARGE_FEED_ITEMS) -> bytes:
    """Return a valid RSS document holding `items` items with sizeable bodies."""
    body = "".join(
        f"<item><title>Post {index}</title><guid>large-{index}</guid>"
        f"<description>{'padding ' * 60}</description></item>"
        for index in range(items)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Large Blog</title><link>{FEED_LINK}</link>{body}"
        "</channel></rss>"
    ).encode()


@asynccontextmanager
async def real_server(body: bytes) -> AsyncIterator[TestServer]:
    """Serve `body` from a genuine aiohttp server, whole and chunk by chunk.

    `aioclient_mock` feeds the entire body into the stream reader and marks it
    EOF before the client reads, so through the mocker one `read()` always
    returns everything - the truncation a real socket produces is invisible to
    it. These tests therefore talk to a real server: `/whole` answers with a
    plain response, `/chunked` writes the body out in 4 KiB pieces.
    """

    async def whole(request: web.Request) -> web.StreamResponse:
        """Answer with the body in one `Response`."""
        return web.Response(body=body, content_type="application/rss+xml")

    async def chunked(request: web.Request) -> web.StreamResponse:
        """Answer with the body written out in small pieces."""
        response = web.StreamResponse()
        await response.prepare(request)
        for start in range(0, len(body), 4096):
            await response.write(body[start : start + 4096])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/whole", whole)
    app.router.add_get("/chunked", chunked)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


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


async def _images(hass: HomeAssistant) -> dict[str, str | None]:
    """Return the extracted image of every item of the images fixture, by key."""
    result = await async_parse_feed(hass, load_feed("feed_images"), FEED_URL)
    return {item.key: item.image for item in result.items}


async def test_the_item_image_is_taken_from_the_first_source_that_has_one(
    hass: HomeAssistant,
) -> None:
    """An image/* enclosure, then Media RSS, then the summary's first <img>.

    Feeds in the wild use all three, so the order is what decides: the fixture's
    first item offers all of them and must answer with the enclosure, and the
    YouTube shape (a thumbnail beside a video) must answer with the thumbnail
    rather than with the clip.
    """
    images = await _images(hass)

    assert images["enclosure-image"] == "https://cdn.example.com/enclosure.jpg"
    assert images["media-thumbnail"] == "https://cdn.example.com/thumb.jpg"
    assert images["media-content-medium"] == "https://cdn.example.com/medium.jpg"
    assert images["media-content-type"] == "https://cdn.example.com/typed.png"
    # neither medium nor type: nothing says it is not a picture
    assert images["media-content-bare"] == "https://cdn.example.com/bare.jpg"
    # the summary is HTML, so its entities are unescaped on the way out
    assert images["summary-img"] == "https://cdn.example.com/in-summary.jpg?w=1&h=2"
    # an <img> without a src does not end the search
    assert images["summary-img-no-src"] == "https://cdn.example.com/second.jpg"


async def test_an_item_without_a_usable_image_reports_none(
    hass: HomeAssistant,
) -> None:
    """Nothing is substituted, and only an absolute http(s) URL counts.

    A placeholder would be worse than the empty field: an automation could not
    tell it from a real picture. A relative or protocol-relative `src` has no
    base to be resolved against here, and a `data:` URI would carry the whole
    picture into every payload and into the recorder.
    """
    images = await _images(hass)

    assert images["no-image"] is None
    # an enclosure is not a picture just because it is an enclosure
    assert images["enclosure-audio"] is None
    # a declared kind is believed
    assert images["media-content-video"] is None
    assert images["summary-img-relative"] is None
    assert images["summary-img-protocol-relative"] is None
    assert images["summary-img-data-uri"] is None


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


@pytest.mark.parametrize("path", ["/whole", "/chunked"])
async def test_a_large_feed_is_read_whole(
    hass: HomeAssistant, socket_enabled: None, path: str
) -> None:
    """A feed over a megabyte arrives complete, from a genuine socket.

    This is the one thing `aioclient_mock` cannot show: it buffers the whole body
    into the stream reader and marks EOF before the client reads, so a single
    `read(n)` returns everything. A real `StreamReader` only waits for its buffer
    to be non-empty, so `read(MAX_FEED_BYTES + 1)` returned about 48 KiB of this
    document (200-odd of its 2000 items) and feedparser announced the truncated
    list as if it were the whole feed - the boundary item with an empty body,
    marked seen forever. Hence the real server here.
    """
    payload = large_feed()
    assert len(payload) > 1_000_000, "the body must exceed one buffered read"

    async with real_server(payload) as server:
        result = await async_fetch_feed(hass, str(server.make_url(path)))

    assert isinstance(result, FetchResult)
    assert len(result.items) == LARGE_FEED_ITEMS
    assert result.items[-1].summary != ""
    assert result.items[-1].key == f"large-{LARGE_FEED_ITEMS - 1}"


async def test_oversized_body_is_refused_by_a_real_server(
    hass: HomeAssistant, socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size cap fires against a real, chunk-by-chunk response.

    The mocker cannot prove this either: it hands the body over in one piece, so
    a cap check that only ever sees the first buffered read passes there while
    being unreachable in production. Against a real socket the pre-fix client
    read 100 KiB of this body and accepted it.
    """
    monkeypatch.setattr(
        "custom_components.rss_notify.client.MAX_FEED_BYTES", 100 * 1024
    )

    async with real_server(large_feed()) as server:
        with pytest.raises(FeedFetchError, match="larger than the 102400 byte limit"):
            await async_fetch_feed(hass, str(server.make_url("/chunked")))


async def test_a_body_naming_a_local_file_is_not_opened(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A response body that is a filesystem path must not be read as one.

    `feedparser.parse` falls through to `open(argument, "rb")` for an argument
    that is neither readable nor a URL, and `bytes` reaches that branch - so a
    hostile or MITM'd feed answering with a path would have Home Assistant
    announce the items of a local XML file. The payload is handed over as a
    stream, which takes feedparser's first branch instead.
    """
    local = tmp_path / "local.xml"
    local.write_bytes(
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<title>LOCAL SECRET</title><link>https://local.example.com/</link>"
        b"<item><title>Local item</title><guid>local-1</guid></item>"
        b"</channel></rss>"
    )

    with pytest.raises(FeedParseError, match="Unable to parse feed"):
        await async_parse_feed(hass, str(local).encode(), FEED_URL)


@pytest.mark.parametrize(
    "url",
    [
        # the protocol-relative form sites embed in <link href>: aiohttp's
        # connector raises a bare AssertionError for it, which `ClientError`
        # does not catch, so it used to escape the config flow unhandled
        "//feeduser:s3cret@example.com/rss",
        "//example.com/rss",
        # opaque URLs: the text scrubber cannot mask every one of them, so they
        # must not reach the transport, whose own message quotes them verbatim
        "mailto:s3cret@example.com",
        "data:text/plain,s3cret",
        "urn:s3cret",
        "ftp://feeduser:s3cret@example.com/rss",
        "feed://example.com/rss?token=s3cret",
        # unparsable: an unterminated IPv6 literal
        "http://[::1",
    ],
)
async def test_a_url_that_is_not_http_is_refused_before_the_fetch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, url: str
) -> None:
    """A non-http(s) URL is a FeedFetchError, quotes no secret and hits no socket."""
    with pytest.raises(FeedFetchError, match="not an http or https address") as raised:
        await async_fetch_feed(hass, url)

    assert "s3cret" not in str(raised.value)
    assert aioclient_mock.call_count == 0


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


def _real_aiohttp_error() -> aiohttp.ClientResponseError:
    """Return the error real aiohttp raises for a 5xx, credentials included.

    `aioclient_mock` fabricates the `request_info` of the errors it raises, so its
    URL is not the one under test - the leak this guards against simply cannot
    occur through the mocker. Real aiohttp puts `request_info.real_url` in the
    message, which is the full URL with its userinfo and query, so the error has
    to be built by hand for the test to mean anything.
    """
    url = URL(SECRET_URL)
    info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    return aiohttp.ClientResponseError(
        info, (), status=HTTPStatus.INTERNAL_SERVER_ERROR, message="Server Error"
    )


async def test_the_traceback_of_a_failed_fetch_leaks_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The logged traceback carries no credentials, not only the message.

    `DataUpdateCoordinator` logs the whole exception chain with `exc_info=True`
    at debug level - the level the troubleshooting docs tell users to turn on -
    and the transport's own message quotes the URL unmasked. So the formatted
    traceback, which is what actually reaches the log, is what has to be clean;
    asserting on `str(err)` alone would miss a chained cause entirely.
    """
    cause = _real_aiohttp_error()
    assert any(secret in str(cause) for secret in SECRET_PARTS), (
        "the fixture must carry the credentials, or this test proves nothing"
    )
    aioclient_mock.get(SECRET_URL, exc=cause)

    with pytest.raises(FeedFetchError) as raised:
        await async_fetch_feed(hass, SECRET_URL)

    logged = "".join(
        traceback.format_exception(
            type(raised.value), raised.value, raised.value.__traceback__
        )
    )
    assert not any(secret in logged for secret in SECRET_PARTS)


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
    """HTML is reduced to unescaped plain text with its breaks intact."""
    assert to_plain_text("") == ""
    assert (
        to_plain_text("<p>Hello&nbsp;&amp; <b>welcome</b></p>\n<p>Bye</p>")
        == "Hello & welcome\n\nBye"
    )


def test_to_plain_text_keeps_line_breaks() -> None:
    """`<br>` and list items become newlines rather than spaces."""
    assert to_plain_text("one<br>two<br/>three") == "one\ntwo\nthree"
    assert to_plain_text("one<BR />two") == "one\ntwo"
    assert (
        to_plain_text("<ul><li>first</li><li>second</li></ul><p>after</p>")
        == "first\nsecond\n\nafter"
    )


def test_to_plain_text_break_shape_is_source_independent() -> None:
    """Paragraph spacing does not depend on how the feed indents its markup."""
    assert to_plain_text("<p>a</p><p>b</p>") == "a\n\nb"
    assert to_plain_text("<p>a</p>\n<p>b</p>") == "a\n\nb"
    assert to_plain_text("<p>a</p>\n\n\n\n<p>b</p>") == "a\n\nb"
    assert to_plain_text("  <div>a</div>   <div>b</div>  ") == "a\n\nb"
    # runs of spaces still collapse, and a break absorbs the spaces around it
    assert to_plain_text("<p>a   b<br>   c   </p>") == "a b\nc"
