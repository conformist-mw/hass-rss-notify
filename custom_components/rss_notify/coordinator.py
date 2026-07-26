"""Coordinator polling one feed: dedup, ordering, initial sync and trickle cap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    FeedError,
    FeedItem,
    FetchResult,
    NotModified,
    async_fetch_feed,
    sort_items_oldest_first,
)
from .const import (
    ATTR_ENTRY_ID,
    ATTR_FEED_TITLE,
    ATTR_FEED_URL,
    ATTR_ITEM_ID,
    ATTR_LINK,
    ATTR_PUBLISHED,
    ATTR_SUMMARY,
    ATTR_SUMMARY_PLAIN,
    ATTR_TITLE,
    CONF_INITIAL_ITEMS,
    CONF_MAX_ITEMS_PER_POLL,
    CONF_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
    DEFAULT_INITIAL_ITEMS,
    DEFAULT_MAX_ITEMS_PER_POLL,
    DEFAULT_TIMEOUT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_NEW_ITEM,
)
from .storage import SeenStore

_LOGGER = logging.getLogger(__name__)


def signal_new_item(entry_id: str) -> str:
    """Return the dispatcher signal carrying new items of one feed."""
    return f"{DOMAIN}_new_item_{entry_id}"


@dataclass(frozen=True, slots=True)
class FeedData:
    """Outcome of a single poll, exposed as coordinator data to consumers."""

    new_items: list[FeedItem]
    feed_title: str
    feed_link: str
    pending: int


class RssFeedCoordinator(DataUpdateCoordinator[FeedData]):
    """Poll one feed and hand out the items that are new since the last poll.

    Items are deduplicated against a persistent seen-set, so a restart never
    re-emits. At most `max_items_per_poll` items are emitted per poll; the rest
    stay unseen and trickle out on subsequent polls. While such a backlog is
    pending, the conditional-GET validators of the response are *not* persisted,
    so the next poll re-fetches a full 200 instead of getting a 304 that would
    strand the trickle.
    """

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, store: SeenStore
    ) -> None:
        """Initialize the coordinator of one feed from its config entry."""
        options = entry.options
        self.url: str = entry.data[CONF_URL]
        self.feed_title: str = entry.data.get(CONF_NAME) or entry.title
        self.feed_link: str = ""
        self.initial_items: int = options.get(CONF_INITIAL_ITEMS, DEFAULT_INITIAL_ITEMS)
        self.max_items_per_poll: int = options.get(
            CONF_MAX_ITEMS_PER_POLL, DEFAULT_MAX_ITEMS_PER_POLL
        )
        self._store = store
        self._session = async_get_clientsession(hass)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.feed_title}",
            update_interval=timedelta(
                minutes=options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            ),
        )

    @property
    def seen_count(self) -> int:
        """Return how many item keys the feed has marked as seen."""
        return self._store.seen_count

    async def _async_update_data(self) -> FeedData:
        """Fetch the feed once and return the items emitted by this poll."""
        try:
            result = await async_fetch_feed(
                self._session,
                self.url,
                etag=self._store.etag,
                last_modified=self._store.last_modified,
                timeout_seconds=DEFAULT_TIMEOUT,
            )
        except FeedError as err:
            raise UpdateFailed(str(err)) from err

        if isinstance(result, NotModified):
            # nothing changed upstream, so nothing is emitted and nothing is
            # persisted; the feed meta of the previous poll is kept
            return self._data(new_items=[], pending=0)

        self.feed_title = result.feed_title or self.feed_title
        self.feed_link = result.feed_link or self.feed_link
        return await self._async_process(result)

    async def _async_process(self, result: FetchResult) -> FeedData:
        """Select the items to emit from a fetched feed and persist the state."""
        ordered = sort_items_oldest_first(result.items)
        first_refresh = self._store.is_new

        if first_refresh:
            # a feed added just now: stay quiet about its backlog, except for
            # the newest `initial_items` which prove the setup works
            emitted = self._newest(ordered, self.initial_items)
            backlog: list[FeedItem] = []
            to_mark = ordered
        else:
            unseen = [item for item in ordered if not self._store.contains(item.key)]
            emitted, backlog = self._split_batch(unseen)
            to_mark = emitted

        # publish before persisting: a crash in between re-emits rather than
        # swallows an item (at-least-once event publication)
        self._emit(emitted)
        await self._async_persist(result, to_mark, hold_validators=bool(backlog))

        _LOGGER.debug(
            "Poll of %s emitted %s item(s), %s pending%s",
            self.url,
            len(emitted),
            len(backlog),
            " (first sync)" if first_refresh else "",
        )
        return self._data(new_items=emitted, pending=len(backlog))

    async def _async_persist(
        self, result: FetchResult, to_mark: list[FeedItem], hold_validators: bool
    ) -> None:
        """Mark items seen and save once, holding back validators if needed.

        The save happens after the items have been handed to consumers, so a
        crash re-emits rather than loses an item (at-least-once publication).
        """
        added = [item.key for item in to_mark if not self._store.contains(item.key)]
        dirty = bool(added) or self._store.is_new
        self._store.add(added)

        if hold_validators:
            # keep the old validators so the next poll gets a full 200 and can
            # continue the trickle instead of being stranded behind a 304
            _LOGGER.debug(
                "Holding back cache validators of %s while a backlog is pending",
                self.url,
            )
        else:
            dirty = dirty or (result.etag, result.last_modified) != (
                self._store.etag,
                self._store.last_modified,
            )
            self._store.etag = result.etag
            self._store.last_modified = result.last_modified

        if dirty:
            await self._store.async_save()

    def _emit(self, items: list[FeedItem]) -> None:
        """Publish items on the bus and to this feed's event entity, in order."""
        signal = signal_new_item(self.config_entry.entry_id)
        for item in items:
            payload = self._payload(item)
            self.hass.bus.async_fire(EVENT_NEW_ITEM, payload)
            async_dispatcher_send(self.hass, signal, payload)

    def _payload(self, item: FeedItem) -> dict[str, Any]:
        """Build the event payload of one item, shared by bus and entity."""
        return {
            ATTR_ENTRY_ID: self.config_entry.entry_id,
            ATTR_FEED_URL: self.url,
            ATTR_FEED_TITLE: self.feed_title,
            ATTR_ITEM_ID: item.key,
            ATTR_TITLE: item.title,
            ATTR_LINK: item.link,
            ATTR_SUMMARY: item.summary,
            ATTR_SUMMARY_PLAIN: item.summary_plain,
            ATTR_PUBLISHED: item.published.isoformat() if item.published else None,
        }

    def _data(self, new_items: list[FeedItem], pending: int) -> FeedData:
        """Wrap the outcome of a poll together with the current feed meta."""
        return FeedData(
            new_items=new_items,
            feed_title=self.feed_title,
            feed_link=self.feed_link,
            pending=pending,
        )

    def _split_batch(
        self, unseen: list[FeedItem]
    ) -> tuple[list[FeedItem], list[FeedItem]]:
        """Split unseen items into the batch to emit now and the backlog."""
        if self.max_items_per_poll <= 0:
            return unseen, []
        return unseen[: self.max_items_per_poll], unseen[self.max_items_per_poll :]

    @staticmethod
    def _newest(ordered: list[FeedItem], count: int) -> list[FeedItem]:
        """Return the `count` newest items, still ordered oldest to newest."""
        if count <= 0:
            return []
        return ordered[-count:]
