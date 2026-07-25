"""Fetching, parsing and normalizing of RSS/Atom feeds."""

from __future__ import annotations

import asyncio
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
from homeassistant.util import dt as dt_util

from .const import DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

USER_AGENT: Final = "HomeAssistant-rss_notify"

_TAG_RE: Final = re.compile(r"<[^>]+>")
_WHITESPACE_RE: Final = re.compile(r"\s+")

# Sort key for items without a publication date, so they come first.
_UNDATED_SORT_KEY: Final = (0, datetime.min.replace(tzinfo=dt_util.UTC))


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
    session: aiohttp.ClientSession,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> FetchResult | NotModified:
    """Fetch a feed with a conditional GET, then parse and normalize it.

    Returns `NotModified` when the server reports the feed is unchanged. Raises
    `FeedFetchError` on transport/HTTP problems and `FeedParseError` when the
    payload is not a usable feed.
    """
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_seconds)
        ) as response:
            if response.status == HTTPStatus.NOT_MODIFIED:
                _LOGGER.debug("Feed %s not modified", url)
                return NotModified()
            response.raise_for_status()
            payload = await response.read()
            new_etag = response.headers.get("ETag")
            new_last_modified = response.headers.get("Last-Modified")
    except TimeoutError as err:
        raise FeedFetchError(f"Timeout fetching feed from {url}") from err
    except aiohttp.ClientError as err:
        raise FeedFetchError(f"Error fetching feed from {url}: {err}") from err

    return await async_parse_feed(
        payload, url=url, etag=new_etag, last_modified=new_last_modified
    )


async def async_parse_feed(
    payload: bytes,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """Parse feed bytes in the executor and normalize the entries."""
    loop = asyncio.get_running_loop()
    parsed = await loop.run_in_executor(None, feedparser.parse, payload)

    if not parsed.entries and (parsed.bozo or not parsed.get("version")):
        reason = parsed.get("bozo_exception") or "no feed items and no feed version"
        raise FeedParseError(f"Unable to parse feed from {url}: {reason}")
    if parsed.bozo:
        # feedparser recovered from the problem, so the entries are still usable.
        _LOGGER.warning(
            "Possible issue parsing feed %s: %s", url, parsed.get("bozo_exception")
        )

    items = [
        item
        for entry in parsed.entries
        if (item := _normalize_entry(entry, url=url)) is not None
    ]
    feed = parsed.get("feed") or {}
    return FetchResult(
        items=items,
        feed_title=(feed.get("title") or "").strip(),
        feed_link=(feed.get("link") or "").strip(),
        etag=etag,
        last_modified=last_modified,
    )


def sort_items_oldest_first(items: Iterable[FeedItem]) -> list[FeedItem]:
    """Sort items oldest to newest, undated items first, preserving feed order."""
    return sorted(items, key=_sort_key)


def to_plain_text(value: str) -> str:
    """Strip HTML tags, unescape entities and collapse whitespace."""
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def _sort_key(item: FeedItem) -> tuple[int, datetime]:
    """Return a sort key placing undated items before dated ones."""
    if item.published is None:
        return _UNDATED_SORT_KEY
    return (1, item.published)


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
        _LOGGER.warning(
            "Skipping feed item without any usable identity in feed %s", url
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
