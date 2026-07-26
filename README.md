# RSS Notify

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
| Burst protection | none | `max_items_per_poll`, remaining items trickle out on later polls |
| Item order | feed order | oldest → newest, so notifications read chronologically |

If your feeds are well behaved, `feedreader` is fine and needs no extra install. This
integration exists for the feeds that are not: missing GUIDs, missing dates, dates that
change, or a publisher that dumps twenty posts at once.

## Requirements

- Home Assistant 2026.7.4 or newer (the minimum HACS enforces for this repository)
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

| Field | Meaning |
| --- | --- |
| URL | The feed address. It is fetched and parsed right away, so a typo or a page that is not a feed is rejected in the dialog. |
| Name | Optional. Defaults to the title the feed reports; the URL is used if it has none. |

The URL is the entry's unique id, so the same feed cannot be added twice.

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
| `initial_items` | 1 | How many of the newest items are announced when the feed is added. `0` sets the feed up completely silently. Only ever applies to the first poll of a feed. |
| `max_items_per_poll` | 10 | Upper bound on announcements per poll. Items above the cap stay unseen and are announced on the following polls, oldest first. `0` means unlimited. |

Changing an option reloads the feed; the seen-set is kept.

## What you get for every new item

Two surfaces carry the same payload, pick whichever suits the automation:

- the bus event `rss_notify_new_item` — full item text
- the feed's `event` entity, event type `new_item` — the same fields as state
  attributes, with `summary` and `summary_plain` cut to 500 characters so the recorder
  does not keep a copy of every article

| Field | Description |
| --- | --- |
| `entry_id` | Config entry id of the feed, handy to filter one feed out of many |
| `feed_url` | Feed address |
| `feed_title` | Feed title |
| `item_id` | The dedup key of the item (its GUID, or its link, or a content hash) |
| `title` | Item title |
| `link` | Item link |
| `summary` | Item description/content as the feed delivers it, usually HTML |
| `summary_plain` | Same text with tags stripped, entities unescaped, whitespace collapsed |
| `published` | Publication time in ISO 8601, or `null` when the feed gives none |

Items are announced oldest first, one bus event and one state change per item, so a
batch of five new posts produces five notifications in reading order.

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
- restarting Home Assistant never replays items that were already announced
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
out.

```yaml
alias: RSS to notification drawer
mode: queued
max: 50
triggers:
  - trigger: state
    entity_id: event.example_blog_new_item
    not_from:
      - unknown
      - unavailable
    not_to:
      - unknown
      - unavailable
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
- **Reload** — three-dot menu → **Reload**, e.g. after a feed changes its URL scheme.
- **Delete** — deleting the entry removes its device, entity and stored seen-set.
  Adding the same feed again starts over, announcing `initial_items` items.

## How dedup works

Every item gets a key: its `guid`/`id` if it has one, otherwise its `link`, otherwise a
SHA-256 fingerprint of title, date and summary. Items with none of those are skipped
with a warning — there is no way to recognise them again.

The keys live in `.storage/rss_notify.<entry_id>` together with the feed's cache
validators. The seen-set is insertion-ordered and pruned to the newest 5000 keys, which
is far above any realistic feed window while keeping the file small.

Two details worth knowing:

- `ETag`/`Last-Modified` are only stored when a poll leaves nothing pending. While
  items are still trickling out under `max_items_per_poll`, the old validators are kept
  on purpose, so the next poll gets a full response instead of a `304` that would
  strand the backlog.
- the seen-set is saved *after* the events are fired, which is what makes publication
  at-least-once rather than at-most-once.

## Troubleshooting

Feed's three-dot menu → **Download diagnostics** reports the feed's options, title,
poll state, how many keys are in the seen-set, how many items are still pending, and
the last fetch result. Feed URLs are masked — userinfo and query values are stripped
before the report is written, so a feed behind basic auth or an access token can be
shared safely.

For more detail, turn on debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.rss_notify: debug
```

Common cases:

- **entity is unavailable** — the last poll failed; the error is in the log and in the
  diagnostics report
- **no events at all** — check `initial_items`: a feed added with `0` stays silent until
  it publishes something new
- **an item arrived twice** — its identity changed between polls (the feed rewrote the
  GUID, the link, or the item text of an item without a GUID), which no dedup can see
  through

## Contributing

Issues and pull requests are welcome:
<https://github.com/conformist-mw/hass-rss-notify/issues>

Development setup:

```shell
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```
