"""Fetching, parsing and normalizing of RSS/Atom feeds."""

from __future__ import annotations

from calendar import timegm
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
import hashlib
import html
from http import HTTPStatus
import io
import logging
import re
from time import struct_time
from typing import Any, Final
from urllib.parse import urlsplit

import aiohttp
import feedparser
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import FETCH_TIMEOUT, MAX_FEED_BYTES
from .redact import redact_url, redact_urls

_LOGGER = logging.getLogger(__name__)

USER_AGENT: Final = "HomeAssistant-rss_notify"

# the only schemes aiohttp can request; anything else is refused before the fetch
# so the transport never sees - and never quotes back - a URL shape it cannot use
HTTP_SCHEMES: Final = frozenset({"http", "https"})

# how much of the body is taken per read; the cap is enforced on the running
# total, so at most one chunk beyond `MAX_FEED_BYTES` is ever held in memory
_READ_CHUNK_BYTES: Final = 64 * 1024

_TAG_RE: Final = re.compile(r"<[^>]+>")
# a summary is HTML, and its <br> and paragraph boundaries carry the author's
# structure; dropping them turns every item into one wall of text
_LINE_END_RE: Final = re.compile(r"<br\s*/?>|</(?:li|tr)\s*>", re.IGNORECASE)
_PARA_END_RE: Final = re.compile(
    r"</(?:p|div|blockquote|pre|h[1-6]|ul|ol|table|section|article)\s*>",
    re.IGNORECASE,
)
# every whitespace character except the newline, so runs of spaces collapse
# without eating the breaks just introduced
_SPACES_RE: Final = re.compile(r"[^\S\n]+")
_AROUND_BREAK_RE: Final = re.compile(r" ?\n ?")
_BLANK_RUN_RE: Final = re.compile(r"\n{3,}")
_IMG_SRC_RE: Final = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*["'](?P<src>[^"']+)["']""", re.IGNORECASE
)

# a feed problem does not heal between polls, so the same warning would repeat
# for as long as the feed is configured; see `_log_feed_problem`
_WARNED: Final[set[str]] = set()


class FeedError(Exception):
    """Base error for feed handling."""


class FeedFetchError(FeedError):
    """Raised when the feed could not be retrieved."""


class FeedParseError(FeedError):
    """Raised when the retrieved document is not a parsable feed."""


@dataclass(frozen=True, slots=True)
class FeedItem:
    """A single normalized feed item."""

    key: str
    title: str
    link: str
    summary: str
    summary_plain: str
    published: datetime | None
    image: str | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A successfully fetched and parsed feed."""

    items: list[FeedItem]
    feed_title: str
    feed_link: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class NotModified:
    """The server answered 304 Not Modified, there is nothing to process."""


async def async_fetch_feed(
    hass: HomeAssistant,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_seconds: int = FETCH_TIMEOUT,
) -> FetchResult | NotModified:
    """Fetch a feed with a conditional GET, then parse and normalize it.

    Returns `NotModified` when the server reports the feed is unchanged. Raises
    `FeedFetchError` on transport/HTTP problems and `FeedParseError` when the
    payload is not a usable feed. Error messages carry the *redacted* URL: they
    are logged and shown in the UI as the failure reason of the entry.
    """
    if not _is_http_url(url):
        # aiohttp answers a non-http(s) URL inconsistently - `ClientError` for
        # `ftp://`, a bare `AssertionError` from the connector for the
        # protocol-relative `//host/path` that sites embed in <link href> - and
        # an `AssertionError` would escape the config flow as an unknown error
        # instead of a form error. Refusing here also keeps an opaque URL
        # (`mailto:`, `data:`, `urn:`) out of the transport's own message.
        raise FeedFetchError(
            f"Feed URL {redact_url(url)} is not an http or https address"
        )

    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        async with async_get_clientsession(hass).get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_seconds)
        ) as response:
            if response.status == HTTPStatus.NOT_MODIFIED:
                _LOGGER.debug("Feed %s not modified", redact_url(url))
                return NotModified()
            response.raise_for_status()
            payload = await _async_read_capped(response, url)
            new_etag = response.headers.get("ETag")
            new_last_modified = response.headers.get("Last-Modified")
    # `from None` on both: the transport exception quotes the raw URL in its own
    # message, and `DataUpdateCoordinator` logs the whole chain with
    # `exc_info=True` at debug level - the very level the troubleshooting docs
    # tell users to turn on. Suppressing the cause keeps the credentials out of
    # that traceback; nothing is lost, because the transport's own text is folded
    # into the redacted message below.
    except TimeoutError:
        raise FeedFetchError(f"Timeout fetching feed from {redact_url(url)}") from None
    except aiohttp.ClientError as err:
        raise FeedFetchError(
            redact_urls(f"Error fetching feed from {url}: {err}")
        ) from None

    return await async_parse_feed(
        hass, payload, url=url, etag=new_etag, last_modified=new_last_modified
    )


def _is_http_url(url: str) -> bool:
    """Return whether `url` is an absolute http(s) address."""
    try:
        return urlsplit(url).scheme in HTTP_SCHEMES
    except ValueError:
        # an unparsable URL (an unterminated IPv6 literal, say) is not one either
        return False


async def _async_read_capped(response: aiohttp.ClientResponse, url: str) -> bytes:
    """Return the whole response body, refusing one that passes the size cap.

    The body has to be accumulated chunk by chunk: `StreamReader.read(n)` returns
    as soon as the buffer holds anything at all rather than reading until `n`
    bytes or EOF, so a single `read(MAX_FEED_BYTES + 1)` truncates every feed
    bigger than one buffered read - silently, since feedparser recovers from a
    document cut mid-`<item>` and yields the entries it got. The cap is checked
    against the running total, so a hostile body is still refused without ever
    being buffered whole.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_FEED_BYTES:
            raise FeedFetchError(
                f"Feed at {redact_url(url)} is larger than "
                f"the {MAX_FEED_BYTES} byte limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def async_parse_feed(
    hass: HomeAssistant,
    payload: bytes,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """Parse feed bytes in the executor and normalize the entries.

    The payload is handed over as a stream, never as `bytes`: `feedparser.parse`
    falls through to `open(argument, "rb")` for an argument that is neither
    readable nor a URL, so a response body that is exactly a filesystem path
    would make it read that local file instead - a feed server could have Home
    Assistant announce the contents of any XML file on the host, or block an
    executor thread forever on a character device, past both the fetch timeout
    and the size cap. A `BytesIO` takes feedparser's first branch.
    """
    parsed = await hass.async_add_executor_job(feedparser.parse, io.BytesIO(payload))

    if not parsed.entries and (parsed.bozo or not parsed.get("version")):
        reason = parsed.get("bozo_exception") or "no feed items and no feed version"
        raise FeedParseError(f"Unable to parse feed from {redact_url(url)}: {reason}")
    if parsed.bozo:
        # feedparser recovered from the problem, so the entries are still usable.
        _log_feed_problem(
            f"bozo:{url}",
            "Possible issue parsing feed %s: %s",
            redact_url(url),
            parsed.get("bozo_exception"),
        )

    items = [
        item
        for entry in parsed.entries
        if (item := _normalize_entry(entry, url=url)) is not None
    ]
    feed = parsed.get("feed") or {}
    # feedparser strips text nodes itself; stripping again costs nothing and
    # keeps the guarantee (a padded title would name the device and the entity)
    return FetchResult(
        items=items,
        feed_title=(feed.get("title") or "").strip(),
        feed_link=(feed.get("link") or "").strip(),
        etag=etag,
        last_modified=last_modified,
    )


def sort_items_oldest_first(items: Iterable[FeedItem]) -> list[FeedItem]:
    """Sort items oldest to newest, undated items first.

    `items` has to be in document order. RSS and Atom documents list the newest
    item first, so items without a publication date are ordered by their
    *reversed* document position: for a feed that dates nothing, the position in
    the document is the only clue about which of its items is the newest one.
    """
    positioned = sorted(enumerate(items), key=_sort_key)
    return [item for _position, item in positioned]


def to_plain_text(value: str) -> str:
    """Strip HTML tags and unescape entities, keeping block breaks as newlines.

    Runs of spaces still collapse, but a `<br>`, list item or paragraph boundary
    survives as a newline. The breaks are inserted before the tags are stripped
    and are normalized afterwards, so the result does not depend on how the feed
    happened to indent its markup: at most one blank line between paragraphs.
    """
    if not value:
        return ""
    text = _PARA_END_RE.sub("\n\n", _LINE_END_RE.sub("\n", value))
    text = html.unescape(_TAG_RE.sub(" ", text))
    text = _AROUND_BREAK_RE.sub("\n", _SPACES_RE.sub(" ", text))
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


def _log_feed_problem(key: str, message: str, *args: Any) -> None:
    """Warn about a recurring feed problem once, then keep it at debug level.

    A defect in a feed is still there on the next poll, so a plain warning would
    be repeated for every poll - thousands of log lines a day at the default
    interval. `key` identifies the problem and the feed it belongs to.
    """
    level = logging.DEBUG if key in _WARNED else logging.WARNING
    _WARNED.add(key)
    _LOGGER.log(level, message, *args)


def _sort_key(positioned: tuple[int, FeedItem]) -> tuple[int, float]:
    """Return a sort key placing undated items before dated ones."""
    position, item = positioned
    if item.published is None:
        # a document lists newest first, so a later position is an older item
        return (0, -position)
    return (1, item.published.timestamp())


def _normalize_entry(entry: Any, url: str) -> FeedItem | None:
    """Convert a feedparser entry into a `FeedItem`, or None when unusable."""
    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    # feedparser backfills `summary` from <content>/<content:encoded> when the
    # entry has no description, so this covers RSS and Atom alike.
    summary = str(entry.get("summary") or "")
    published_raw = entry.get("published") or entry.get("updated") or ""

    key = _item_key(
        guid=(entry.get("id") or "").strip(),
        link=link,
        title=title,
        published_raw=published_raw,
        summary=summary,
    )
    if key is None:
        _log_feed_problem(
            f"no-identity:{url}",
            "Skipping feed item without any usable identity in feed %s",
            redact_url(url),
        )
        return None

    return FeedItem(
        key=key,
        title=title,
        link=link,
        summary=summary,
        summary_plain=to_plain_text(summary),
        published=_entry_published(entry),
        image=_entry_image(entry, summary=summary),
    )


def _entry_image(entry: Any, summary: str) -> str | None:
    """Return the item's picture, or None when the entry carries none usable.

    The first *usable* candidate wins, and only an absolute http(s) URL is
    usable: a relative or protocol-relative `src` has no base a notification
    could resolve it against, and a `data:` URI would carry the whole picture
    into every payload and into the recorder.

    Nothing is substituted when a feed offers no image. A placeholder would be
    worse than the empty field, because an automation cannot tell one from a
    real picture, while `None` lets it choose between sending a photo and
    sending text.
    """
    for candidate in _image_candidates(entry, summary):
        url = html.unescape(candidate).strip()
        if _is_http_url(url):
            return url
    return None


def _image_candidates(entry: Any, summary: str) -> Iterator[str]:
    """Yield the image URLs an entry offers, best source first."""
    for enclosure in entry.get("enclosures") or ():
        # the only enclosure kind that is a picture; a podcast episode or a PDF
        # attachment is an enclosure too
        if str(enclosure.get("type") or "").startswith("image/"):
            yield str(enclosure.get("href") or "")

    # Media RSS, what YouTube and most news feeds carry. A thumbnail is a
    # picture by definition; `media:content` is also used for video and audio,
    # so a declared kind is believed and only an undeclared one is assumed to be
    # an image.
    for thumbnail in entry.get("media_thumbnail") or ():
        yield str(thumbnail.get("url") or "")
    for content in entry.get("media_content") or ():
        medium = str(content.get("medium") or "")
        mime = str(content.get("type") or "")
        if medium == "image" or mime.startswith("image/") or not (medium or mime):
            yield str(content.get("url") or "")

    # last resort: the publisher's own HTML. An <img> without a `src` is skipped
    # rather than ending the search, so a lead image below a spacer is found.
    for match in _IMG_SRC_RE.finditer(summary):
        yield match.group("src")


def _item_key(
    guid: str, link: str, title: str, published_raw: str, summary: str
) -> str | None:
    """Return the dedup key for an item: guid, then link, then a fingerprint."""
    if guid:
        return guid
    if link:
        return link
    if not (title or published_raw or summary):
        return None
    fingerprint = "|".join((title, published_raw, summary))
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _entry_published(entry: Any) -> datetime | None:
    """Return the publication time of an entry as an aware UTC datetime."""
    parsed: struct_time | None = entry.get("published_parsed") or entry.get(
        "updated_parsed"
    )
    if parsed is None:
        return None
    return dt_util.utc_from_timestamp(timegm(parsed))
