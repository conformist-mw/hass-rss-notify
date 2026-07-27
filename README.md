# RSS Notify

[![CI](https://github.com/conformist-mw/hass-rss-notify/actions/workflows/ci.yml/badge.svg)](https://github.com/conformist-mw/hass-rss-notify/actions/workflows/ci.yml)
[![HACS: custom repository](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://www.hacs.xyz/docs/faq/custom_repositories/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.7.0%2B-41BDF5.svg)](https://www.home-assistant.io/)

A Home Assistant custom integration that watches RSS/Atom feeds and fires an event for
every **new** item, so you can wire up any notification method you like (Telegram, the
mobile app, persistent notifications, TTS) with a regular automation.

New items are recognised by their **GUID/link** against a persistent seen-set, not by
their timestamp — feeds that republish items, backdate them, or omit dates entirely do
not produce duplicate notifications.

- one config entry per feed, with per-feed polling options
- an `event` entity per feed for UI automations, plus a `rss_notify_new_item` bus event
- adding a feed is quiet by design: you get the newest item as proof it works, not the
  whole archive
- conditional GET (ETag / `Last-Modified`) so unchanged feeds cost one cheap request

## How it compares to the built-in `feedreader`

Home Assistant ships a [`feedreader`](https://www.home-assistant.io/integrations/feedreader/)
integration that solves the same problem with a different dedup model. It stores **one
timestamp per feed** and publishes every entry whose `updated`/`published` date is newer
than that timestamp.

| | `feedreader` | `rss_notify` |
| --- | --- | --- |
| Dedup state | newest timestamp seen per feed | seen-set of item keys (GUID → link → content fingerprint) |
| Items without a date | never published after the first poll | published like any other item |
| Items with a backdated or rewritten date | missed, or published again | published exactly once |
| First poll of a new feed | publishes the whole current window (up to 20 items) | publishes the newest `initial_items` (default 1), rest silently marked seen |
| Poll interval | fixed at 1 hour | per feed, default 5 minutes |
| Burst protection | caps a poll at `max_entries` (default 20) and discards the rest | `max_items_per_poll`, remaining items trickle out on later polls |
| Item order | feed order | oldest → newest, so notifications read chronologically |

If your feeds are well behaved, `feedreader` is fine and needs no extra install. This
integration exists for the feeds that are not: missing GUIDs, missing dates, dates that
change, or a publisher that dumps twenty posts at once.

## Requirements

- Home Assistant 2026.7.0 or newer (the minimum HACS enforces for this repository)
- [HACS](https://hacs.xyz/) for the recommended install path

## Installation

### HACS (custom repository)

1. HACS → three-dot menu → **Custom repositories**
2. Repository: `https://github.com/conformist-mw/hass-rss-notify`, type: **Integration**
3. Add, then find **RSS Notify** in HACS and download it
4. Restart Home Assistant

### Manual

Copy `custom_components/rss_notify` into your `config/custom_components/` directory and
restart Home Assistant.

## Adding a feed

**Settings → Devices & services → Add integration → RSS Notify**

The dialog has two steps:

| Step | Field | Meaning |
| --- | --- | --- |
| 1 | URL | The feed address. It is fetched and parsed right away, so a typo or a page that is not a feed is rejected in the dialog. |
| 2 | Name | Pre-filled with the title that fetch found in the feed — its host if the feed reports none. Edit it or accept it; clearing it keeps the suggestion. It can be changed later by renaming the feed. |

The name is asked for after the URL because that is what lets it be filled in for you: the
title belongs to the feed document, so it is only known once the feed has been fetched.

The URL is the entry's unique id, so the same feed cannot be added twice, and it cannot be
changed afterwards — see [Managing feeds](#managing-feeds).

Each feed becomes a service device holding one entity:

```text
event.<feed name>_new_item
```

Right after setup the feed's current items are marked as seen and the newest one is
announced, so you immediately see the plumbing work without being flooded by the
archive.

### Options

**Settings → Devices & services → RSS Notify → Configure**

| Option | Default | Meaning |
| --- | --- | --- |
| `update_interval` | 5 minutes | How often the feed is polled. |
| `initial_items` | 1 | How many of the newest items are announced when the feed is added. `0` sets the feed up completely silently. Only ever applies to the first poll of a feed, and is never capped by `max_items_per_poll`. |
| `max_items_per_poll` | 10 | Upper bound on announcements per *regular* poll. Items above the cap stay unseen and are announced on the following polls, oldest first. `0` means unlimited. The first poll of a feed ignores the cap: it always announces `initial_items` items. |

Changing an option reloads the feed; the seen-set is kept.

## What you get for every new item

Two surfaces carry the same payload, pick whichever suits the automation:

- the bus event `rss_notify_new_item` — full item text
- the feed's `event` entity, event type `new_item` — the same fields as state
  attributes plus `event_type: new_item`, with `summary` and `summary_plain` cut to 500
  characters so the recorder does not keep a copy of every article

An event entity holds the *last* event it fired, and Home Assistant **restores** that
across a restart: right after a restart the entity is back with the timestamp and the
attributes of the item it announced before, and writing that restored state counts as a
state change with no previous state. A state trigger therefore fires once per restart,
carrying an item that was already announced — and `not_from`/`not_to` do not filter it,
because "no previous state" is not `unknown`. The entity example below rules it out with a
`trigger.from_state is not none` condition; the bus event is not affected and needs no
guard.

| Field | Description |
| --- | --- |
| `entry_id` | Config entry id of the feed, handy to filter one feed out of many |
| `feed_url` | Feed address |
| `feed_title` | The feed's name in Home Assistant: the name confirmed when the feed was added — the feed's own title unless you edited it (its host if the feed reports no title). It is the name shown in the UI, so renaming the feed there changes it too; a rename by the *publisher* never does |
| `item_id` | The dedup key of the item (its GUID, or its link, or a content hash) |
| `title` | Item title |
| `link` | Item link |
| `summary` | Item description/content as the feed delivers it, usually HTML |
| `summary_plain` | Same text with tags stripped, entities unescaped, whitespace collapsed |
| `published` | Publication time in ISO 8601, or `null` when the feed gives none |

Items are announced oldest first, one bus event and one state change per item, so a
batch of five new posts produces five notifications in reading order. Items the feed
gives no date for sort *before* every dated item, so for a feed that mixes the two the
order is: all undated items (bottom of the document first), then the dated ones oldest
to newest.

Formatting is deliberately left to you: the payload is raw feed text, so if you send it
with `parse_mode: markdown` or `html`, escape it in your template — an item title with a
stray `*` or `<` is otherwise enough to break the message.

## Delivery guarantee

The contract is **at-least-once publication of the Home Assistant event**, not delivery
of your notification.

An item is marked seen right after its event has been fired. If the automation that
sends the notification fails — Telegram down, no internet, phone unreachable — the item
is *not* announced again. What the integration does guarantee:

- a crash or restart *between* firing the event and persisting the seen-set re-announces
  the item rather than swallowing it
- restarting Home Assistant never re-fires the bus event for an item that was already
  announced. Home Assistant does restore the *entity's* last event, so an automation
  triggering on the entity needs the `trigger.from_state is not none` condition of the
  example below to ignore that one state change
- an unreachable or broken feed leaves the seen-set untouched; the entity goes
  unavailable and the next successful poll continues where it left off

So retries belong in the automation. The pattern below queues one run per item
(`mode: queued`, so a burst is not dropped) and retries the send a few times before
giving up:

```yaml
alias: RSS to Telegram (with retries)
mode: queued
max: 50
triggers:
  - trigger: event
    event_type: rss_notify_new_item
actions:
  - repeat:
      count: 3
      sequence:
        - action: telegram_bot.send_message
          continue_on_error: true
          response_variable: sent
          data:
            title: "{{ trigger.event.data.feed_title }}"
            message: |-
              {{ trigger.event.data.title }}
              {{ trigger.event.data.link }}
        # a false condition stops the run, so a successful send ends the loop
        - condition: template
          value_template: "{{ sent is not defined }}"
        - delay: "00:01:00"
```

## Example automations

### Event entity → persistent notification

The UI-friendly variant: pick the feed's entity as the trigger, no event type to type
out. Two guards are needed and both matter — `not_to` keeps a failed poll (which turns the
entity `unavailable`) from triggering with no item, and the condition drops the state
change a restart produces when Home Assistant restores the entity's last event.

```yaml
alias: RSS to notification drawer
mode: queued
max: 50
triggers:
  - trigger: state
    entity_id: event.example_blog_new_item
    not_to:
      - unknown
      - unavailable
conditions:
  # a restart re-adds the entity carrying the item it last announced, and that
  # state change has no previous state at all - unlike a real new item, which
  # always follows one (`unknown` on a fresh feed, `unavailable` after a failure)
  - condition: template
    value_template: "{{ trigger.from_state is not none }}"
actions:
  - action: persistent_notification.create
    data:
      title: "{{ trigger.to_state.attributes.title }}"
      message: >-
        {{ trigger.to_state.attributes.summary_plain }}

        {{ trigger.to_state.attributes.link }}
```

### Bus event → mobile app

```yaml
alias: RSS to phone
mode: queued
max: 50
triggers:
  - trigger: event
    event_type: rss_notify_new_item
actions:
  - action: notify.mobile_app_my_phone
    data:
      title: "{{ trigger.event.data.feed_title }}"
      message: "{{ trigger.event.data.title }}"
      data:
        url: "{{ trigger.event.data.link }}"
```

### One automation for many feeds

`entry_id` filters the bus event without a template:

```yaml
triggers:
  - trigger: event
    event_type: rss_notify_new_item
    event_data:
      entry_id: 01JABCDEF0123456789ABCDEF
```

Or route by feed in the action:

```yaml
actions:
  - choose:
      - conditions: >-
          {{ trigger.event.data.feed_url == 'https://example.com/rss' }}
        sequence:
          - action: notify.mobile_app_my_phone
            data:
              message: "{{ trigger.event.data.title }}"
```

## Managing feeds

- **Pause** — disable the config entry (feed's three-dot menu → **Disable**). Polling
  stops and the seen-set is kept, so re-enabling announces what the feed published in
  the meantime — once each, capped by `max_items_per_poll` — and nothing from before.
- **Reload** — three-dot menu → **Reload**, e.g. to rebuild a feed that is stuck after a
  long outage without waiting for the next poll. A reload keeps the seen-set and re-reads
  the stored settings; it does not re-read the feed's URL from anywhere else.
- **Rename** — three-dot menu → **Rename**. The new name reloads the feed and becomes the
  device name, the entity's friendly name and the `feed_title` of everything announced
  from then on. The entity id keeps its original name; change that in the entity settings
  if you want.
- **Delete** — deleting the entry removes its device, entity and stored seen-set.
  Adding the same feed again starts over, announcing `initial_items` items.

The URL of a feed is fixed when it is added: the options dialog only offers the three
polling options, so a feed that moved to a new address has to be deleted and added again.
That starts its seen-set over, so expect `initial_items` announcements from the new entry.

## How dedup works

Every item gets a key: its `guid`/`id` if it has one, otherwise its `link`, otherwise a
SHA-256 fingerprint of title, date and summary. Items with none of those are skipped
with a warning — there is no way to recognise them again.

The keys live in `.storage/rss_notify.<entry_id>` together with the feed's cache
validators. The seen-set is insertion-ordered and pruned to the newest 5000 keys, which
keeps the file small; a feed that lists more items than that in one document keeps all of
them instead (see below).

Some feeds list the same item twice in one document — a rewritten entry, or an archive
merged into the current window. Repeats inside a single fetch are collapsed to their
first occurrence, so such an item is announced once, not twice.

Items **without** a publication date are ordered by their position in the document,
bottom-up: RSS and Atom documents list the newest item first, so for a feed that dates
nothing at all that position is the only clue about which item is the newest one. Adding
such a feed therefore announces the item at the top of the document.

Pruning only ever drops keys of items the feed no longer lists: the keys of the current
document are refreshed on every poll and are exempt from the cap. An item that stays in
the document — a pinned post, a full archive, a feed longer than 5000 items — can
therefore never age out and be announced a second time.

A few more details worth knowing:

- `ETag`/`Last-Modified` are only stored when a poll leaves nothing pending. While items
  are still trickling out under `max_items_per_poll`, the stored validators are *cleared*,
  so the next poll asks unconditionally and gets a full response instead of a `304` that
  would strand the backlog. They are stored again with the poll that drains the backlog.
- the seen-set is saved *after* the events are fired, which is what makes publication
  at-least-once rather than at-most-once.
- items held back by `max_items_per_poll` are not queued anywhere. They are simply left
  unseen, and the next poll finds them in the document again — which is what makes the
  trickle survive a restart without a second piece of stored state. The trade-off: an item
  that leaves the feed's window before a poll gets to it is never announced. That needs a
  feed publishing more than `max_items_per_poll` items per interval *and* a short window,
  so if you follow such a feed, raise the cap or shorten `update_interval`.
- every request is bounded by a fixed 30 second timeout and a 16 MiB response limit;
  neither is configurable. The body is read in 64 KiB pieces and the limit is applied to
  the running total, so a feed larger than it is refused outright rather than truncated,
  and an oversized response is never buffered whole.
- the feed URL has to be an `http://` or `https://` address. Anything else — a
  protocol-relative `//host/feed.xml`, a `feed://` or `ftp://` scheme, a bare hostname — is
  rejected when the feed is added, with "Failed to connect to the feed URL".

## Troubleshooting

Feed's three-dot menu → **Download diagnostics** reports the feed's options, title,
poll state, how many keys are in the seen-set, how many items are still pending, and
the last fetch result. Feed URLs are masked in it — userinfo is replaced, the query string
is replaced *whole* (keys included, because a token is as often a bare key as a value) and
the fragment is dropped, in the URL, in the feed's name and inside the last error message.
Log lines mask the URL the same way, errors and debug output alike.

Two limits of that masking are worth knowing before you share a report:

- a secret that lives in the URL **path** (`https://example.com/feed/<token>`) is not
  masked, because a path cannot be told apart from a credential
- `feed_url` is part of every announced item, so it is also written to the recorder
  database and shown as the device's link — verbatim, credentials included

For more detail, turn on debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.rss_notify: debug
```

To poll a feed right now instead of waiting out `update_interval`, call
`homeassistant.update_entity` on its event entity — it triggers a full coordinator
refresh:

```yaml
action: homeassistant.update_entity
target:
  entity_id: event.example_blog_new_item
```

Common cases:

- **entity is unavailable** — the last poll failed; the error is in the log and in the
  diagnostics report
- **`cannot_connect` for a feed that works in the browser** — every request goes out with
  `User-Agent: HomeAssistant-rss_notify`, which some CDNs and publishers answer with a
  `403`. There is no override; such a feed needs a proxy in front of it
- **no events at all** — check `initial_items`: a feed added with `0` stays silent until
  it publishes something new
- **an item arrived twice** — its identity changed between polls: the feed rewrote the
  GUID, the link, or (for an item without either) the item text, which no dedup can see
  through. A repeat *within* one fetch is handled and never announced twice

## Contributing

Issues and pull requests are welcome:
<https://github.com/conformist-mw/hass-rss-notify/issues>

Development setup — Python 3.14 or newer, which is what the pinned Home Assistant test
stack requires:

```shell
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Coverage of `custom_components/rss_notify` is expected to stay at 100% statement and
branch:

```shell
.venv/bin/pytest --cov=custom_components/rss_notify --cov-branch --cov-report=term-missing
```

## License

MIT — the full text is in [`LICENSE`](LICENSE).
