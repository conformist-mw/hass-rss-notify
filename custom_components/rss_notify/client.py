"""Fetching, parsing and normalizing of RSS/Atom feeds."""

from __future__ import annotations

from calendar import timegm
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import html
from http import HTTPStatus
import logging
import re
from time import struct_time
from typing import Any, Final

import aiohttp
import feedparser
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import FETCH_TIMEOUT, MAX_FEED_BYTES
from .redact import redact_url, redact_urls

_LOGGER = logging.getLogger(__name__)

USER_AGENT: Final = "HomeAssistant-rss_notify"

_TAG_RE: Final = re.compile(r"<[^>]+>")
_WHITESPACE_RE: Final = re.compile(r"\s+")

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
            # a hostile or misconfigured URL must not buffer an unbounded body
            # into memory: one byte over the cap is enough to reject it
            payload = await response.content.read(MAX_FEED_BYTES + 1)
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

    if len(payload) > MAX_FEED_BYTES:
        raise FeedFetchError(
            f"Feed at {redact_url(url)} is larger than the {MAX_FEED_BYTES} byte limit"
        )

    return await async_parse_feed(
        hass, payload, url=url, etag=new_etag, last_modified=new_last_modified
    )


async def async_parse_feed(
    hass: HomeAssistant,
    payload: bytes,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """Parse feed bytes in the executor and normalize the entries."""
    parsed = await hass.async_add_executor_job(feedparser.parse, payload)

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
    """Strip HTML tags, unescape entities and collapse whitespace."""
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()


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
    )


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
