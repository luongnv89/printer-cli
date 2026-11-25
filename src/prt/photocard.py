"""4×6 photo prints for the photo tray: a four-frame photo-booth strip and an instant-camera print.

``strip`` lays four photos out in a column, the way a photo booth prints them, with a handwritten caption at the
foot; the column is printed twice side by side, so the sheet cut down its middle (the dashed line) gives two
2×6 in strips. ``instant`` prints one photo, cropped square, in a wide white frame with a handwritten caption
under it, like an instant-camera print (Polaroid-style).

Both are drawn on :data:`prt.paper.PHOTO_4X6` (upright, 300 dpi) and print from the photo tray. The caption is
set in the bundled Caveat font; letters it lacks print without their accents, and text it cannot print at all,
such as emoji or Chinese, is refused before anything is drawn.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw

from prt import fonts
from prt.booth import _grow, _photo, load_photo
from prt.drivers.base import PrinterError
from prt.layout import Cuts, _clean
from prt.paper import PHOTO_4X6
from prt.vintage import _char_in, _downscale, _fit_face

W, H = PHOTO_4X6.size  # 1200 x 1800
FRAMES = 4  # photos on a strip
STRIP_CAPTION_LIMIT = 60
INSTANT_CAPTION_LIMIT = 80
PHOTO_LIMIT = 1200  # longest side a photo is kept at once decoded: it never prints larger
STRIP_KIND = "photo-booth strip"
INSTANT_KIND = "instant photo"
CUT_HINT = "Cut the sheet along the dashed line for two strips."


def today() -> str:
    """Today's date as the default caption, e.g. "24 November 2026"."""
    now = time.localtime()
    return f"{now.tm_mday} {time.strftime('%B %Y', now)}"


def check_caption(text: str, limit: int, exit_code: int = 2) -> str:
    """The caption with its whitespace cleaned, refused when longer than ``limit`` characters or written in a
    script the handwriting font cannot print."""
    value = _clean(text, keep_newlines=False)
    if len(value) > limit:
        raise PrinterError(
            f"caption is too long ({len(value)} characters, max {limit})",
            hint="shorten the caption",
            exit_code=exit_code,
        )
    face = fonts.face("hand", 40)
    unprintable = sorted({c for c in value if c != "?" and _char_in(face, c) == "?"})
    if unprintable:
        shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
        raise PrinterError(
            f"caption has characters the handwriting font cannot print: {shown}",
            hint="write it in Latin, Greek or Cyrillic letters (no emoji)",
            exit_code=exit_code,
        )
    return value


def load_photos(paths: Sequence[Path]) -> list[Image.Image]:
    """Decode every photo now (exit code 3 on the first bad one), each shrunk to the most it prints at."""
    return [_downscale(load_photo(path), PHOTO_LIMIT) for path in paths]


def check_count(n: int) -> None:
    """A strip takes exactly ``FRAMES`` photos (exit code 2 otherwise)."""
    if n != FRAMES:
        raise PrinterError(
            f"a photo-booth strip takes {FRAMES} photos, got {n}",
            hint=f"give {FRAMES} photos, in the order of the frames from the top (repeat one to use it twice)",
            exit_code=2,
        )


def _caption(
    d: ImageDraw.ImageDraw,
    text: str,
    box: tuple[int, int, int, int],
    size: int,
    min_size: int,
    fill,
    cut: Cuts,
    role: str = "hand",
) -> None:
    """``text`` in handwriting, on up to two lines centred in ``box``; a caption that does not fit at
    ``min_size`` is cut with "…" and named in ``cut``."""
    if not text:
        return
    x0, y0, x1, y1 = box
    font, s, lines, cut_caption = _fit_face(text, role, size, min_size, x1 - x0, 2)
    if cut_caption:
        cut["caption"] = cut_caption
    lead = round(s * 1.15)
    y = y0 + max(0, (y1 - y0 - lead * len(lines)) // 2)
    for line in lines:
        d.text(((x0 + x1) / 2, y + lead * 0.8), line, font=font, fill=fill, anchor="ms")
        y += lead


# --- the strip ---------------------------------------------------------------------------------

STRIP_W = W // 2  # one 2 in strip
STRIP_SIDE = 50  # white either side of the frames
STRIP_TOP = 60
FRAME_GAP = 24
STRIP_FOOT = 210  # the caption under the frames, and the white below it
FRAME_W = STRIP_W - 2 * STRIP_SIDE
FRAME_H = (H - STRIP_TOP - (FRAMES - 1) * FRAME_GAP - STRIP_FOOT) // FRAMES
STRIP_INK = (34, 34, 34)
FRAME_LINE = (200, 200, 200)
CUT_LINE = (175, 175, 175)


def frame_boxes(x0: int = 0) -> list[tuple[int, int, int, int]]:
    """The four photo frames of the strip that starts at ``x0``, from the top."""
    left = x0 + STRIP_SIDE
    return [
        (left, STRIP_TOP + i * (FRAME_H + FRAME_GAP), left + FRAME_W, STRIP_TOP + i * (FRAME_H + FRAME_GAP) + FRAME_H)
        for i in range(FRAMES)
    ]


def _dashed(d: ImageDraw.ImageDraw, x: int, dash: int = 22, gap: int = 18) -> None:
    for y in range(0, H, dash + gap):
        d.line([(x, y), (x, min(H, y + dash))], fill=CUT_LINE, width=2)


def strip(photos: Sequence[Image.Image], caption: str, mono: bool = False) -> tuple[Image.Image, Cuts]:
    """The strip sheet: the four ``photos`` in a column with ``caption`` under them, twice side by side, and a
    dashed cut line between the two. With ``mono`` the photos print in black and white. Also returns the fields
    cut with "…" to fit (only ever ``caption``)."""
    check_count(len(photos))
    sheet = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(sheet)
    cut: Cuts = {}
    for x0 in (0, STRIP_W):
        for photo, box in zip(photos, frame_boxes(x0)):
            d.rectangle(_grow(box, 2), outline=FRAME_LINE, width=2)
            _photo(sheet, photo, box, mono=mono)
        foot = (x0 + STRIP_SIDE, H - STRIP_FOOT + 16, x0 + STRIP_W - STRIP_SIDE, H - 50)
        _caption(d, caption, foot, 66, 40, STRIP_INK, cut, role="hand-bold")
    _dashed(d, STRIP_W)
    return sheet, cut


# --- the instant print -------------------------------------------------------------------------

INSTANT_SIDE = 84  # the frame either side of and above the photo
PHOTO_BOX = (INSTANT_SIDE, INSTANT_SIDE, W - INSTANT_SIDE, W - INSTANT_SIDE)  # square
CAPTION_BOX = (INSTANT_SIDE + 30, PHOTO_BOX[3] + 40, W - INSTANT_SIDE - 30, H - 220)
FRAME = (252, 251, 246)  # the white of the frame
SHADE = (222, 219, 210)  # the edge of the photo, a little sunk into the frame
PEN = (30, 38, 78)  # blue-black marker
TILT = 1.5  # degrees: the caption is written a little uphill, by hand


def instant(photo: Image.Image, caption: str) -> tuple[Image.Image, Cuts]:
    """The instant print: ``photo`` cropped square in a white frame, ``caption`` in handwriting under it, a
    little tilted. Also returns the fields cut with "…" to fit (only ever ``caption``)."""
    sheet = Image.new("RGB", (W, H), FRAME)
    d = ImageDraw.Draw(sheet)
    d.rectangle(_grow(PHOTO_BOX, 3), fill=SHADE)
    _photo(sheet, photo, PHOTO_BOX, mono=False)
    cut: Cuts = {}
    x0, y0, x1, y1 = CAPTION_BOX
    layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    _caption(ImageDraw.Draw(layer), caption, (0, 0, x1 - x0, y1 - y0), 124, 64, (*PEN, 255), cut)
    layer = layer.rotate(TILT, resample=Image.Resampling.BICUBIC)
    sheet.paste(layer, (x0, y0), layer)
    return sheet, cut
