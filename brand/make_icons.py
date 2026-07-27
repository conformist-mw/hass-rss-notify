"""Draw the brand icon this integration is published under.

The icons live in `home-assistant/brands`, not in this repository - Home
Assistant and HACS load them from `brands.home-assistant.io`, so a PNG shipped
here would never be displayed. This script is kept so the artwork can be
regenerated (a dark variant, a retina size, a colour change) instead of being
re-drawn by hand, and `icon.png` / `icon@2x.png` next to it are exactly what was
submitted. See `README.md` in this directory.

Pillow is the only requirement and it is a development-only one, deliberately
not added to `requirements_test.txt`:

    pip install pillow && python brand/make_icons.py

The artwork is drawn at eight times the target size and downsampled, which is
what smooths its edges - `ImageDraw` anti-aliases nothing on its own.
"""

from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw

# every coordinate below is in this design space, scaled up by `SUPERSAMPLE`
CANVAS: Final = 256
SUPERSAMPLE: Final = 8

# brands asks for a 256x256 `icon.png` and a 512x512 `icon@2x.png`
SIZES: Final = {"icon.png": 256, "icon@2x.png": 512}

CORNER_RADIUS: Final = 56
GRADIENT_TOP: Final = (251, 155, 59, 255)
GRADIENT_BOTTOM: Final = (232, 89, 12, 255)
WHITE: Final = (255, 255, 255, 255)

RGBA: Final = "RGBA"


def _box(
    center_x: float, center_y: float, radius: float
) -> tuple[float, float, float, float]:
    """Return the device-pixel bounding box of `radius` around a design point."""
    return (
        (center_x - radius) * SUPERSAMPLE,
        (center_y - radius) * SUPERSAMPLE,
        (center_x + radius) * SUPERSAMPLE,
        (center_y + radius) * SUPERSAMPLE,
    )


def _tile() -> Image.Image:
    """Return the rounded square the mark sits on, filled with the gradient.

    The tile fills the whole canvas: brands wants the image trimmed to the
    artwork, so the only transparent pixels are the four rounded corners.
    """
    size = CANVAS * SUPERSAMPLE
    gradient = Image.linear_gradient("L").resize((size, size))
    tile = Image.composite(
        Image.new(RGBA, (size, size), GRADIENT_BOTTOM),
        Image.new(RGBA, (size, size), GRADIENT_TOP),
        gradient,
    )
    corners = Image.new("L", (size, size), 0)
    ImageDraw.Draw(corners).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=CORNER_RADIUS * SUPERSAMPLE, fill=255
    )
    tile.putalpha(corners)
    return tile


def _dot(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    center_y: float,
    radius: float,
    colour: tuple[int, int, int, int] = WHITE,
) -> None:
    """Draw a filled circle."""
    draw.ellipse(_box(center_x, center_y, radius), fill=colour)


def _wave(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    center_y: float,
    radius: float,
    width: float,
) -> None:
    """Draw one quarter arc of the broadcast mark, north to east, round-capped.

    `ImageDraw.arc` strokes *inward* from its bounding box rather than around a
    centreline, so the box is half a stroke wider than `radius` - otherwise the
    caps drawn at `radius` sit proud of the arc as two blobs.
    """
    draw.arc(
        _box(center_x, center_y, radius + width / 2),
        start=270,
        end=360,
        fill=WHITE,
        width=round(width * SUPERSAMPLE),
    )
    for cap_x, cap_y in ((center_x, center_y - radius), (center_x + radius, center_y)):
        _dot(draw, cap_x, cap_y, width / 2)


def _bell(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    center_y: float,
    half_height: float,
    colour: tuple[int, int, int, int],
) -> None:
    """Draw a bell: a domed shoulder, a flared skirt, a crown and a clapper."""
    width = half_height * 0.62
    top = center_y - half_height
    shoulder = top + width
    draw.pieslice(_box(center_x, shoulder, width), start=180, end=360, fill=colour)
    hem = center_y + half_height * 0.34
    draw.polygon(
        [
            ((center_x - width) * SUPERSAMPLE, shoulder * SUPERSAMPLE),
            ((center_x - width * 1.42) * SUPERSAMPLE, hem * SUPERSAMPLE),
            ((center_x + width * 1.42) * SUPERSAMPLE, hem * SUPERSAMPLE),
            ((center_x + width) * SUPERSAMPLE, shoulder * SUPERSAMPLE),
        ],
        fill=colour,
    )
    _dot(draw, center_x, top - width * 0.12, width * 0.3, colour)
    _dot(draw, center_x, center_y + half_height * 0.78, width * 0.4, colour)


def draw_icon() -> Image.Image:
    """Return the icon: the broadcast mark, badged with a bell.

    The broadcast mark says "feed" at a glance and the badge says the feed is
    watched, which is the whole integration. Nothing here borrows Home Assistant
    branding: brands forbids that for a custom integration, since it would read
    as an official one.
    """
    icon = _tile()
    draw = ImageDraw.Draw(icon)

    # the mark is nudged down and left to clear the badge; its origin is the dot
    origin_x, origin_y, scale = 64.0, 192.0, 0.88
    stroke = 25.0
    _wave(draw, origin_x, origin_y, 56 * scale, stroke)
    _wave(draw, origin_x, origin_y, 108 * scale, stroke)
    _dot(draw, origin_x, origin_y, 17 * scale)

    badge_x, badge_y = 190.0, 66.0
    _dot(draw, badge_x, badge_y, 53)
    _bell(draw, badge_x, badge_y - 2, 32, GRADIENT_BOTTOM)
    return icon


def main() -> None:
    """Write every size brands asks for next to this script."""
    icon = draw_icon()
    here = Path(__file__).parent
    for filename, size in SIZES.items():
        icon.resize((size, size), Image.LANCZOS).save(
            here / filename, format="PNG", optimize=True
        )


if __name__ == "__main__":
    main()
