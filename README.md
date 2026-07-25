# RSS Notify

A Home Assistant custom integration that watches RSS/Atom feeds and fires an event for
every **new** item, so you can wire up any notification method you like (Telegram, the
mobile app, persistent notifications, TTS) with a regular automation.

New items are recognised by their **GUID/link** against a persistent seen-set, not by
their timestamp — feeds that republish items, backdate them, or omit dates entirely do
not produce duplicate notifications.

Status: work in progress.

## Installation

HACS custom repository: `https://github.com/conformist-mw/hass-rss-notify`

## Documentation

Full configuration walkthrough and example automations are added as the integration is
built out.
