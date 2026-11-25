"""``prt keepalive``: a small patch of every ink colour on an otherwise blank A4 sheet.

An inkjet left idle for days dries ink in its nozzles, and a dried nozzle clogs. Printing a little of every colour
every few days keeps each nozzle firing. The patch is solid cyan, magenta, yellow and black (each fires its own
cartridge whatever the driver does) and red, green and blue (two inks laid together), then light tints and greys:
on a printer with light, grey or photo inks, the driver's colour management is what decides whether they print with
those, so the tints give it the chance.
"""

from __future__ import annotations

import time

from PIL import Image, ImageDraw, ImageFont

from prt.paper import A4

KIND = "keep-alive patch"
INKS = ("cyan", "magenta", "yellow", "black")  # the cartridges every printer fires for the solid patches
# Name and colour of each square, row by row. The tints are about 30 % of a solid ink, the greys 25 % and 50 %.
PATCHES = (
    (
        ("cyan", (0, 255, 255)),
        ("magenta", (255, 0, 255)),
        ("yellow", (255, 255, 0)),
        ("black", (0, 0, 0)),
        ("red", (255, 0, 0)),
        ("green", (0, 160, 0)),
        ("blue", (0, 0, 255)),
    ),
    (
        ("light cyan", (178, 255, 255)),
        ("light magenta", (255, 178, 255)),
        ("light grey", (192, 192, 192)),
        ("mid grey", (128, 128, 128)),
    ),
)
MARGIN = A4.mm(15)
SQUARE = A4.mm(18)
GAP = A4.mm(3)
LABEL = A4.mm(5)  # room under each square for its name
TOP = MARGIN + A4.mm(12)  # the first row, under the heading


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def square(row: int, col: int) -> tuple[int, int, int, int]:
    """The box (left, top, right, bottom, inclusive) of the square in ``row``, ``col`` of ``PATCHES``."""
    x = MARGIN + col * (SQUARE + GAP)
    y = TOP + row * (SQUARE + GAP + LABEL)
    return x, y, x + SQUARE - 1, y + SQUARE - 1


def compose() -> Image.Image:
    """The keep-alive page: a heading with the date, then the patch in the top-left corner of a white A4 sheet."""
    page = Image.new("RGB", A4.size, "white")
    draw = ImageDraw.Draw(page)
    draw.text((MARGIN, MARGIN), f"prt keepalive · {time.strftime('%Y-%m-%d')}", fill=(90, 90, 90), font=_font(26))
    label = _font(16)
    for r, row in enumerate(PATCHES):
        for c, (name, rgb) in enumerate(row):
            box = square(r, c)
            draw.rectangle(box, fill=rgb)
            draw.text((box[0], box[3] + A4.mm(1)), name, fill=(90, 90, 90), font=label)
    return page
