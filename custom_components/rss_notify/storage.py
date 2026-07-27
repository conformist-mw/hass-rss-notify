"""Persistent per-feed state: seen item keys and conditional-GET validators."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import islice
import logging
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import MAX_SEEN_KEYS, STORAGE_KEY_PREFIX, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

_DATA_SEEN: Final = "seen"
_DATA_ETAG: Final = "etag"
_DATA_LAST_MODIFIED: Final = "last_modified"


def storage_key(entry_id: str) -> str:
    """Return the `.storage` key holding the state of one feed."""
    return f"{STORAGE_KEY_PREFIX}.{entry_id}"


class SeenStore:
    """Seen item keys and cache validators of a single feed.

    The seen-set is insertion-ordered and pruned to the newest `MAX_SEEN_KEYS`
    entries on save, which keeps the storage file bounded.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the store of a config entry without loading it."""
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, storage_key(entry_id)
        )
        # a dict is used as an insertion-ordered set with O(1) lookups
        self._seen: dict[str, None] = {}
        # the conditional-GET validators to persist on the next save
        self.etag: str | None = None
        self.last_modified: str | None = None
        self._listed: set[str] = set()
        self._is_new = True

    @property
    def is_new(self) -> bool:
        """Return True when the feed had no persisted state to load.

        Used to tell a genuinely new feed (initial sync) from a restart with an
        existing seen-set, where nothing may be re-emitted.
        """
        return self._is_new

    @property
    def seen_count(self) -> int:
        """Return the number of keys currently in the seen-set."""
        return len(self._seen)

    async def async_load(self) -> None:
        """Load the persisted state, replacing anything held in memory."""
        data = await self._store.async_load()
        self._is_new = data is None
        if data is None:
            data = {}
        self._seen = dict.fromkeys(data.get(_DATA_SEEN) or ())
        self.etag = data.get(_DATA_ETAG)
        self.last_modified = data.get(_DATA_LAST_MODIFIED)

    def contains(self, key: str) -> bool:
        """Return True when the item key was already seen."""
        return key in self._seen

    def add(self, keys: Iterable[str]) -> None:
        """Mark item keys as seen, keeping their insertion order."""
        for key in keys:
            self._seen.setdefault(key, None)

    def touch(self, keys: Iterable[str]) -> None:
        """Record the keys the feed currently lists as the newest ones.

        Keys the feed still lists have to outlive the ones it has dropped:
        pruning removes the oldest keys, and dropping the key of an item that is
        still in the document would announce that item a second time. Known keys
        are therefore moved to the newest end of the set, and pruning skips them
        altogether - a document holding more items than the cap would otherwise
        keep pruning items it still lists. Keys that are not in the seen-set are
        ignored: they are not seen yet.
        """
        self._listed = set()
        for key in keys:
            self._listed.add(key)
            if key in self._seen:
                del self._seen[key]
                self._seen[key] = None

    async def async_save(self) -> None:
        """Persist the current state, pruning the seen-set to its cap."""
        self._prune()
        await self._store.async_save(
            {
                _DATA_SEEN: list(self._seen),
                _DATA_ETAG: self.etag,
                _DATA_LAST_MODIFIED: self.last_modified,
            }
        )
        self._is_new = False

    async def async_remove(self) -> None:
        """Delete the storage file of this feed and forget its state."""
        await self._store.async_remove()
        self._seen = {}
        self._listed = set()
        self.etag = None
        self.last_modified = None
        self._is_new = True

    def _prune(self) -> None:
        """Drop the oldest keys above the cap, in memory and on disk alike."""
        excess = len(self._seen) - MAX_SEEN_KEYS
        if excess <= 0:
            return
        stale = [key for key in self._seen if key not in self._listed]
        dropped = list(islice(stale, excess))
        _LOGGER.debug("Pruning %s seen keys from %s", len(dropped), self._store.key)
        for key in dropped:
            del self._seen[key]
