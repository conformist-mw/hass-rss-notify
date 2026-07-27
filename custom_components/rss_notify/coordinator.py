"""Coordinator polling one feed: dedup, ordering, initial sync and trickle cap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import (
    TimestampDataUpdateCoordinator,
    UpdateFailed,
)

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
    CONF_UPDATE_INTERVAL,
    DEFAULT_INITIAL_ITEMS,
    DEFAULT_MAX_ITEMS_PER_POLL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_NEW_ITEM,
)
from .redact import redact_url
from .storage import SeenStore

_LOGGER = logging.getLogger(__name__)


def signal_new_item(entry_id: str) -> str:
    """Return the dispatcher signal carrying new items of one feed."""
    return f"{DOMAIN}_new_item_{entry_id}"


@dataclass(frozen=True, slots=True)
class FeedData:
    """Outcome of a single poll, exposed as coordinator data to consumers."""

    new_items: list[FeedItem]
    pending: int


class RssFeedCoordinator(TimestampDataUpdateCoordinator[FeedData]):
    """Poll one feed and hand out the items that are new since the last poll.

    Items are deduplicated against a persistent seen-set, so a restart never
    re-emits. At most `max_items_per_poll` items are emitted per poll; the rest
    stay unseen and trickle out on subsequent polls. While such a backlog is
    pending, the conditional-GET validators are cleared instead of updated, so
    the next poll is unconditional and re-fetches a full 200 rather than being
    stranded behind a 304.
    """

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, store: SeenStore
    ) -> None:
        """Initialize the coordinator of one feed from its config entry."""
        options = entry.options
        self.url: str = entry.data[CONF_URL]
        # every log line quotes the masked URL: a log is shared as readily as a
        # diagnostics report, and the URL commonly carries a token
        self._log_url: str = redact_url(self.url)
        # the entry title is the feed's name: the config flow puts the user's
        # name (or the feed's own title, or its host) there, and HA keeps it in
        # step with a rename in the UI. The device, the entity and the payload
        # all report this one value, so they can never disagree.
        self.feed_title: str = entry.title
        self.feed_link: str = ""
        self.initial_items: int = options.get(CONF_INITIAL_ITEMS, DEFAULT_INITIAL_ITEMS)
        self.max_items_per_poll: int = options.get(
            CONF_MAX_ITEMS_PER_POLL, DEFAULT_MAX_ITEMS_PER_POLL
        )
        self.store = store
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.feed_title}",
            update_interval=timedelta(
                minutes=options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            ),
        )

    async def _async_update_data(self) -> FeedData:
        """Fetch the feed once and return the items emitted by this poll."""
        try:
            result = await async_fetch_feed(
                self.hass,
                self.url,
                etag=self.store.etag,
                last_modified=self.store.last_modified,
            )
        except FeedError as err:
            raise UpdateFailed(str(err)) from err

        if isinstance(result, NotModified):
            # nothing changed upstream, so nothing is emitted and nothing is
            # persisted; a backlog stays pending and keeps being reported as such
            return FeedData(new_items=[], pending=self.data.pending if self.data else 0)

        self.feed_link = result.feed_link or self.feed_link
        return await self._async_process(result)

    async def _async_process(self, result: FetchResult) -> FeedData:
        """Select the items to emit from a fetched feed and persist the state."""
        ordered = self._drop_repeated_keys(sort_items_oldest_first(result.items))
        first_refresh = self.store.is_new

        if first_refresh and not ordered:
            # a feed that is empty when it is added has nothing to sync yet.
            # Persisting now would end its "new" state, so the items it
            # publishes later would arrive as a capped steady-state batch
            # instead of the promised `initial_items` announcement.
            _LOGGER.debug(
                "Feed %s is empty, keeping the initial sync pending", self._log_url
            )
            return FeedData(new_items=[], pending=0)

        if first_refresh:
            # a feed added just now: stay quiet about its backlog, except for
            # the newest `initial_items` which prove the setup works
            emitted = self._newest(ordered, self.initial_items)
            backlog: list[FeedItem] = []
            to_mark = ordered
        else:
            unseen = [item for item in ordered if not self.store.contains(item.key)]
            emitted, backlog = self._split_batch(unseen)
            to_mark = emitted

        # publish before persisting: a crash in between re-emits rather than
        # swallows an item (at-least-once event publication)
        self._mark_recovered()
        self._emit(emitted)
        # keys the feed still lists must not age out of the seen-set before the
        # ones it has dropped, or pruning would re-announce an item still listed
        self.store.touch(item.key for item in ordered)
        await self._async_persist(result, to_mark, clear_validators=bool(backlog))

        _LOGGER.debug(
            "Poll of %s emitted %s item(s), %s pending%s",
            self._log_url,
            len(emitted),
            len(backlog),
            " (first sync)" if first_refresh else "",
        )
        return FeedData(new_items=emitted, pending=len(backlog))

    async def _async_persist(
        self, result: FetchResult, to_mark: list[FeedItem], clear_validators: bool
    ) -> None:
        """Mark items seen and save once, clearing the validators if needed.

        The save happens after the items have been handed to consumers, so a
        crash re-emits rather than loses an item (at-least-once publication). The
        store reports whether either write changed anything, so a poll that
        found nothing new costs no disk write at all.
        """
        dirty = self.store.add(item.key for item in to_mark) or self.store.is_new

        if clear_validators:
            # the stored validators are cleared, not kept: the next poll has to
            # get the full document to continue the trickle, and a server may
            # answer 304 for a validator it still considers current (a coarse
            # Last-Modified, or a 200 served with an unchanged ETag)
            _LOGGER.debug(
                "Clearing cache validators of %s while a backlog is pending",
                self._log_url,
            )
        validators = (
            (None, None) if clear_validators else (result.etag, result.last_modified)
        )
        # the validator write must happen either way, so it goes first: `or` would
        # skip it if the added keys had already decided the save
        if self.store.set_validators(*validators) or dirty:
            await self.store.async_save()

    def _mark_recovered(self) -> None:
        """Flag this poll as successful before any item is published.

        `CoordinatorEntity.available` follows `last_update_success`, and the base
        class only sets it once `_async_update_data` has returned. Publishing
        while it is still False after an earlier failed poll would make every
        per-item state write land as `unavailable`, dropping the item instead of
        announcing it.
        """
        if not self.last_update_success:
            self.last_update_success = True
            # logged here because the base class stays quiet about a flag it
            # finds already set
            _LOGGER.info("Fetching %s data recovered", self.name)

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

    def _drop_repeated_keys(self, items: list[FeedItem]) -> list[FeedItem]:
        """Return the items of one fetch with repeated keys collapsed.

        A feed may list the same item twice (a rewritten entry, a merged archive).
        The seen-set is only consulted before the batch is emitted, so without
        this an item repeated inside a single document would be emitted twice.
        First occurrence wins.
        """
        unique: dict[str, FeedItem] = {}
        for item in items:
            unique.setdefault(item.key, item)
        if len(unique) != len(items):
            _LOGGER.debug(
                "Feed %s listed %s item(s) with a key it uses more than once",
                self._log_url,
                len(items) - len(unique),
            )
        return list(unique.values())

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
