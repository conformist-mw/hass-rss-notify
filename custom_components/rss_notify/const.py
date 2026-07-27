"""Constants for the RSS Notify integration."""

from typing import Final

DOMAIN: Final = "rss_notify"

# the entry's data key (`CONF_URL`) and the flow's name field (`CONF_NAME`) are
# the ones `homeassistant.const` already defines; only the keys that are specific
# to this integration live here

# Config entry option keys
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_INITIAL_ITEMS: Final = "initial_items"
CONF_MAX_ITEMS_PER_POLL: Final = "max_items_per_poll"

# Option defaults
DEFAULT_UPDATE_INTERVAL: Final = 5  # minutes
DEFAULT_INITIAL_ITEMS: Final = 1
DEFAULT_MAX_ITEMS_PER_POLL: Final = 10  # 0 = unlimited

# Fetching (both fixed, not exposed as options)
FETCH_TIMEOUT: Final = 30  # seconds
MAX_FEED_BYTES: Final = 16 * 1024 * 1024  # a feed document larger than this is refused

# Persistent seen-store
STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = DOMAIN
MAX_SEEN_KEYS: Final = 5000

# Bus event fired for every new feed item
EVENT_NEW_ITEM: Final = "rss_notify_new_item"

# Event entity event type
EVENT_TYPE_NEW_ITEM: Final = "new_item"

# Event entity attributes are truncated for recorder hygiene
ATTR_MAX_LENGTH: Final = 500

# Event payload keys
ATTR_ENTRY_ID: Final = "entry_id"
ATTR_FEED_URL: Final = "feed_url"
ATTR_FEED_TITLE: Final = "feed_title"
ATTR_ITEM_ID: Final = "item_id"
ATTR_TITLE: Final = "title"
ATTR_LINK: Final = "link"
ATTR_SUMMARY: Final = "summary"
ATTR_SUMMARY_PLAIN: Final = "summary_plain"
ATTR_PUBLISHED: Final = "published"
