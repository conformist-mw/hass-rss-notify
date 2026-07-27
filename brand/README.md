# Brand icon

`icon.png` (256×256) and `icon@2x.png` (512×512) are the icon this integration is
published under, and `make_icons.py` is what draws them.

**They are not loaded from this repository.** Home Assistant and HACS resolve an
integration's icon to `https://brands.home-assistant.io/rss_notify/icon.png`, so until the
domain exists in [`home-assistant/brands`](https://github.com/home-assistant/brands) HACS
shows "icon not available" — no file placed in this repository, under any name, changes
that. The copies here exist so the artwork can be regenerated and so it is clear what was
submitted.

## Submitting them

```shell
gh repo fork home-assistant/brands --clone --remote=false
cd brands
git switch -c add-rss-notify
mkdir -p custom_integrations/rss_notify
cp ../hass-rss-notify/brand/icon.png    custom_integrations/rss_notify/icon.png
cp ../hass-rss-notify/brand/icon@2x.png custom_integrations/rss_notify/icon@2x.png
git add custom_integrations/rss_notify
git commit -m "Add icon for rss_notify custom integration"
gh pr create --repo home-assistant/brands \
  --title "Add icon for rss_notify custom integration" \
  --body "Custom integration: https://github.com/conformist-mw/hass-rss-notify"
```

The directory name has to be the manifest `domain`, `rss_notify`, exactly. A logo
(`logo.png`, landscape) is optional; a square icon is what HACS and the integrations page
use. Dark variants are optional too and this one needs none — white on orange holds up in
both themes.

Once the PR is merged, drop `ignore: brands` from the HACS step in
`.github/workflows/ci.yml`.

## Requirements the PR is checked against

- PNG only, and no symlinks under `custom_integrations/`.
- `icon.png` exactly 256×256, `icon@2x.png` exactly 512×512, both square.
- Trimmed to the artwork: no empty margin. The tile here fills the canvas, so the only
  transparent pixels are the rounded corners.
- No Home Assistant branding in a custom integration's icon — it would read as an
  official one.
