"""Photo booth: a photo of a person and a short story, laid out as a newspaper front page or a themed card.

Rendered as an A4 JPEG at 150 dpi, like the test page, so it prints straight over IPP.
Newspaper styles: ``metro`` (colour tabloid, two columns) and ``daily`` (black-and-white broadsheet,
three columns). Themed styles: ``wizard`` (an ornate trading card) and ``wanted`` (a weathered
pirate poster), which reuse the same story fields for the name, label, subtitle, lore and footer.
Without a story each style uses its own default (``default_story``).
"""

from __future__ import annotations

import io
import json
import math
import random
import sys
import time
from dataclasses import dataclass, fields, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from prt.drivers.base import PrinterError
from prt.layout import (BOTTOM, CONTENT_W, Cuts, _clean, _columns, _columns_height, _fit, _glyphs, _in_source,
                        _lead, _printable, _text_block)
from prt.layout import _col_width, _ellipsize, _rows, _wrap  # noqa: F401  (still reachable as booth.<name>)
from prt.testpage import H, MARGIN, W, _font

# --- the story -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Story:
    headline: str
    story: str
    kicker: str = ""
    caption: str = ""
    byline: str = ""


FIELDS = tuple(f.name for f in fields(Story))
REQUIRED = ("headline", "story")
# Upper bounds that keep a story on one page; text that still overflows its box ends in "…",
# and compose() names the fields that were cut, with how much of each was printed, so the command can say so.
LIMITS = {"headline": 120, "story": 2500, "kicker": 40, "caption": 200, "byline": 80}
MAX_STORY_FILE = 64 * 1024
STDIN = "-"  # a story file or article source of "-" is read from standard input

DEFAULT_STORY = Story(
    kicker="Local news",
    headline="Local Star Steps Into the Spotlight",
    story=("A familiar face stepped up to the photo booth today and smiled for the camera, "
           "drawing a small but enthusiastic crowd. Witnesses described the moment as "
           "\"effortless\" and \"exactly what the afternoon needed\".\n"
           "Asked about the secret behind the look, our subject kept it modest: good light, "
           "a steady pose and a little confidence. The picture was printed on the spot.\n"
           "Editors confirm that no further questions will be taken at this time."),
    caption="Pictured: today's guest at the prt photo booth.",
    byline="By the prt newsroom",
)

# The themed styles read the same fields differently: headline is the name, kicker the small label
# at the top, caption the subtitle (wizard) or reward line (wanted), story the lore or description,
# and byline the footer.
WIZARD_STORY = Story(
    kicker="Legendary Wizard",
    headline="The Spellbound Guest",
    caption="Keeper of the Photo Booth",
    story=("Appeared in a flash of light at the photo booth and has not stopped smiling since. "
           "Said to brew a strong cup of tea and an even stronger pose.\n"
           "Special power: turns any ordinary afternoon into a little bit of magic."),
    byline="Booth wizard series · card No. 1",
)

WANTED_STORY = Story(
    kicker="Dead or Alive",
    headline="The Grinning Captain",
    caption="Reward 1,000,000 Gold",
    story=("Wanted for stealing the show at the photo booth and smiling without a licence. "
           "Last seen striking a fearless pose and refusing to blink.\n"
           "Approach with a camera. Do not attempt to out-smile."),
    byline="By order of the Harbour Watch",
)

CERTIFICATE_STORY = Story(
    kicker="Certificate of Achievement",
    headline="The Photo Booth Guest",
    caption="For the Brightest Smile of the Day",
    story=("In recognition of an outstanding performance in front of the camera: a steady pose, perfect timing "
           "and a smile that lit up the whole room."),
    byline="The prt Awards Committee",
)

SPORTS_STORY = Story(
    kicker="Booth City Flashes",
    headline="The Shutter Star",
    caption="Forward · No. 23",
    story=("Height: just right. Reach: the whole room.\n"
           "Season: 12 smiles, 9 poses, 0 blinks.\n"
           "Scouting report: quick off the mark and never misses a photo opportunity."),
    byline="Photo booth series · rookie card",
)

MISSING_STORY = Story(
    kicker="Have you seen this cat?",
    headline="Whiskers",
    caption="Reward: a lifetime supply of treats",
    story=("Last seen near the photo booth, posing for the camera and refusing to share the spotlight. "
           "Answers to her name, a rattled treat bag or the sound of a tin opener.\n"
           "Friendly, fluffy and very photogenic. Please do not chase: just take a picture and call."),
    byline="Call 555-0134",
)

MOVIE_STORY = Story(
    kicker="One smile. One take. One legend.",
    headline="The Photo Booth",
    caption="Coming soon to a printer near you",
    story=("prt Pictures presents a Self-Timer production · starring Today's Guest as Themselves · "
           "music by The Shutter Clicks · edited by One Lucky Take · directed by The Flash"),
    byline="Rated PG for scenes of extreme smiling",
)

DEFAULT_STORIES = {"metro": DEFAULT_STORY, "daily": DEFAULT_STORY, "wizard": WIZARD_STORY, "wanted": WANTED_STORY,
                   "certificate": CERTIFICATE_STORY, "sports": SPORTS_STORY, "missing": MISSING_STORY,
                   "movie": MOVIE_STORY}
# What each style prints, for messages such as the confirmation prompt.
KINDS = {"metro": "metro newspaper page", "daily": "daily newspaper page", "wizard": "wizard card",
         "wanted": "wanted poster", "certificate": "certificate", "sports": "sports card",
         "missing": "missing-pet poster", "movie": "movie poster"}


def default_story(style: str) -> Story:
    """The story a style prints when none is given."""
    return DEFAULT_STORIES[style]


def check_story(story: Story, exit_code: int) -> Story:
    """Normalise whitespace and enforce the required fields, length limits and printable characters."""
    clean = {}
    for name in FIELDS:
        value = _clean(getattr(story, name), keep_newlines=name == "story")
        if name in REQUIRED and not value:
            raise PrinterError(f"the story has an empty {name}", hint=f"give the {name} some text",
                               exit_code=exit_code)
        if len(value) > LIMITS[name]:
            raise PrinterError(f"{name} is too long ({len(value)} characters, max {LIMITS[name]})",
                               hint=f"shorten the {name} so it fits on the page", exit_code=exit_code)
        unprintable = sorted({c for c in value if _glyphs(c) is None})
        if unprintable:
            shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
            raise PrinterError(f"{name} has characters the page font cannot print: {shown}",
                               hint="write it in Latin letters (accents are printed without their marks)",
                               exit_code=exit_code)
        clean[name] = value
    return Story(**clean)


def read_stdin(limit: int) -> bytes:
    """Standard input as bytes, at most ``limit + 1`` of them: a longer result means the input is over ``limit``."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise PrinterError("cannot read standard input: there is none", hint="pipe the input in, or give a file",
                           exit_code=3)
    try:
        return stream.read(limit + 1)
    except (OSError, ValueError) as exc:  # ValueError: stdin was closed
        raise PrinterError(f"cannot read standard input: {exc}", hint="pipe the input in, or give a file",
                           exit_code=3) from exc


def load_story(path: Path) -> Story:
    """Read a story JSON file: {"headline", "story", "kicker"?, "caption"?, "byline"?}; ``-`` reads stdin."""
    if str(path) == STDIN:
        name = kind = "story on stdin"
        content = read_stdin(MAX_STORY_FILE)
        too_big = len(content) > MAX_STORY_FILE
    else:
        if not path.is_file():
            raise PrinterError(f"story file not found: {path}", hint="check the path", exit_code=3)
        name, kind = f"story file {path.name}", "story file"
        too_big = path.stat().st_size > MAX_STORY_FILE
        content = b"" if too_big else path.read_bytes()
    if too_big:
        raise PrinterError(f"{name} is larger than {MAX_STORY_FILE // 1024} KiB",
                           hint="a booth story is a few short paragraphs", exit_code=3)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8 JSON", exit_code=3) from exc
    return parse_story(text, name, kind)


def parse_story(text: str, name: str = "story", kind: str = "story") -> Story:
    """A story from its JSON text, checked as a story file is; ``name`` and ``kind`` say where it came from in
    error messages (e.g. ``story file story.json`` and ``story file``)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrinterError(f"{name} is not valid JSON: {exc.msg} (line {exc.lineno})",
                           hint='expected {"headline": "...", "story": "..."}', exit_code=3) from exc
    if not isinstance(data, dict):
        raise PrinterError(f"{name} must hold a JSON object",
                           hint='expected {"headline": "...", "story": "..."}', exit_code=3)
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise PrinterError(f"unknown field(s) in {kind}: {', '.join(unknown)}",
                           hint="allowed fields: " + ", ".join(FIELDS), exit_code=3)
    missing = [field for field in REQUIRED if field not in data]
    if missing:
        raise PrinterError(f"{name} is missing {' and '.join(missing)}",
                           hint='expected {"headline": "...", "story": "..."}', exit_code=3)
    wrong = [field for field, value in data.items() if not isinstance(value, str)]
    if wrong:
        raise PrinterError(f"{kind} fields must be text: {', '.join(sorted(wrong))}",
                           hint='quote the values, e.g. "byline": "By Jo"', exit_code=3)
    return check_story(Story(**data), exit_code=3)


def with_overrides(base: Story, **changes: str | None) -> Story:
    """Apply command-line text over a story; ``None`` keeps the base value."""
    return check_story(replace(base, **{k: v for k, v in changes.items() if v is not None}), exit_code=2)


# --- the photo -------------------------------------------------------------------------------


PHOTO_HINT = "use a JPEG, PNG, WebP, GIF, BMP or TIFF image"


def load_photo(path: Path) -> Image.Image:
    """Fully decode the photo now, so a bad file fails before anything reaches the printer."""
    if not path.is_file():
        raise PrinterError(f"photo not found: {path}", hint="check the path", exit_code=3)
    try:
        with Image.open(path) as img:
            img.load()
            img = ImageOps.exif_transpose(img)
            if img.has_transparency_data:  # e.g. a cut-out portrait: see-through parts print as paper
                img = Image.alpha_composite(Image.new("RGBA", img.size, "white"), img.convert("RGBA"))
            return img.convert("RGB")
    except UnidentifiedImageError as exc:
        raise PrinterError(f"cannot read photo {path.name}: not a supported image format",
                           hint=PHOTO_HINT, exit_code=3) from exc
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise PrinterError(f"cannot read photo {path.name}: {exc}", hint=PHOTO_HINT, exit_code=3) from exc


# --- layout helpers --------------------------------------------------------------------------


def _today() -> str:
    now = time.localtime()
    return time.strftime(f"%A, %B {now.tm_mday}, %Y", now)


def _photo_box(photo: Image.Image, top: int, height: int, max_w: int) -> tuple[int, int, int, int]:
    """A box of fixed height whose width follows the photo's shape (portraits stay upright), centred."""
    width = max(max_w // 2, min(max_w, round(height * photo.width / photo.height)))
    x0 = (W - width) // 2
    return x0, top, x0 + width, top + height


def _photo(page: Image.Image, photo: Image.Image, box: tuple[int, int, int, int], mono: bool) -> None:
    """Crop to fill the box and paste it.

    Only photos taller than the box lose height (full-length portraits); those are cut mostly from
    the bottom, so the head stays in the picture.
    """
    x0, y0, x1, y1 = box
    fitted = ImageOps.fit(photo, (x1 - x0, y1 - y0), method=Image.Resampling.LANCZOS, centering=(0.5, 0.1))
    if mono:
        fitted = ImageOps.autocontrast(ImageOps.grayscale(fitted), cutoff=1).convert("RGB")
    page.paste(fitted, (x0, y0))


# --- styles ----------------------------------------------------------------------------------

INK = (0, 0, 0)
RED = (206, 17, 38)
WHITE = (255, 255, 255)


METRO_PHOTO, METRO_PHOTO_MAX = 560, 900  # photo height: normal, and grown into space a short story leaves
METRO_BODY = 28


def _metro(page: Image.Image, photo: Image.Image, story: Story) -> Cuts:
    """Colour tabloid: reversed black masthead, red accents, shouting headline, full-width photo, 2 columns."""
    d = ImageDraw.Draw(page)
    cut: Cuts = {}
    left, right = MARGIN, W - MARGIN
    y = MARGIN
    d.rectangle([left, y, right, y + 150], fill=INK)
    d.text((left + 30, y + 16), "METRO", font=_font(112), fill=WHITE, stroke_width=3, stroke_fill=WHITE)
    small = _font(26)
    for i, text in enumerate((_today(), "FREE · EVERY DAY")):
        d.text((right - 30 - small.getlength(text), y + 42 + i * 40), text, font=small, fill=WHITE)
    y += 150
    d.rectangle([left, y, right, y + 12], fill=RED)
    y += 36
    if story.kicker.strip():
        font, size, [label], cut_kicker = _fit(story.kicker.upper(), 30, 20, CONTENT_W - 32, 1)
        d.rectangle([left, y, left + font.getlength(label) + 32, y + 46], fill=RED)
        d.text((left + 16, y + 6 + (30 - size) // 2), label, font=font, fill=WHITE)
        y += 62
        if cut_kicker:
            cut["kicker"] = cut_kicker
    font, size, lines, cut_headline = _fit(story.headline.upper(), 80, 48, CONTENT_W, 2)
    if cut_headline:  # too long for two lines even at the smallest size: allow a third
        font, size, lines, cut_headline = _fit(story.headline.upper(), 48, 48, CONTENT_W, 3)
    y = _text_block(d, left, y, lines, font, size, INK, bold=2) + 14
    if cut_headline:
        cut["headline"] = cut_headline
    if story.byline:
        by_font, by_size, by_lines, cut_byline = _fit(story.byline.upper(), 26, 20, CONTENT_W, 1)
    # A short story leaves paper to spare: the photo grows into it (up to a 4:3 crop of a wide photo).
    # The caption bar is reckoned at its largest, as the caption is fitted to the final photo width.
    max_w = CONTENT_W - 8  # the frame is drawn 4 px outside the photo, so it lines up with the masthead
    base = _photo_box(photo, y, METRO_PHOTO, max_w)  # the narrowest the photo can be
    caption_lines = len(_fit(story.caption, 24, 20, base[2] - base[0] - 40, 2)[2]) if story.caption else 0
    below = 4 + (caption_lines * _lead(24) + 20 if caption_lines else 0) + 26 + \
        (_lead(by_size) + 6 if story.byline else 0)
    room = BOTTOM - 24 - y - below - _columns_height(story.story, CONTENT_W, 2, 44, METRO_BODY)
    grow_to = min(METRO_PHOTO_MAX, round(max_w / min(photo.width / photo.height, 4 / 3)))
    px0, py0, px1, py1 = _photo_box(photo, y, max(METRO_PHOTO, min(grow_to, room)), max_w)
    _photo(page, photo, (px0, py0, px1, py1), mono=False)
    d.rectangle([px0 - 4, py0 - 4, px1 + 4, py1 + 4], outline=INK, width=10)
    y = py1 + 4
    if story.caption:
        font, size, lines, cut_caption = _fit(story.caption, 24, 20, px1 - px0 - 40, 2)
        bar = len(lines) * _lead(size) + 20
        d.rectangle([px0 - 4, y, px1 + 4, y + bar], fill=INK)
        _text_block(d, px0 + 20, y + 10, lines, font, size, WHITE)
        y += bar
        if cut_caption:
            cut["caption"] = cut_caption
    y += 26
    if story.byline:
        y = _text_block(d, left, y, by_lines, by_font, by_size, RED, bold=1) + 6
        if cut_byline:
            cut["byline"] = cut_byline
    d.rectangle([left, BOTTOM - 8, right, BOTTOM], fill=RED)
    if story_cut := _columns(d, story.story, (left, y, right, BOTTOM - 24), 2, 44, METRO_BODY, 16, INK):
        cut["story"] = story_cut
    return cut


GREY = (95, 95, 95)
DAILY_INK = (25, 25, 25)
DAILY_PHOTO, DAILY_PHOTO_MAX = 460, 820  # photo height: normal, and grown into space a short story leaves
DAILY_BODY, DAILY_BODY_MAX = 24, 28  # body type: normal, and grown when the photo is already at its largest


def _daily(page: Image.Image, photo: Image.Image, story: Story) -> Cuts:
    """Black-and-white broadsheet: centred masthead, double rules, grey photo with a mat, 3 ruled columns."""
    d = ImageDraw.Draw(page)
    cut: Cuts = {}
    left, right = MARGIN, W - MARGIN
    y = MARGIN
    tiny = _font(20)
    d.text((left, y), "VOL. I · No. 1", font=tiny, fill=GREY)
    edition = "LATE EDITION"
    d.text((right - tiny.getlength(edition), y), edition, font=tiny, fill=GREY)
    y += 34
    d.line([(left, y), (right, y)], fill=DAILY_INK, width=1)
    y += 14
    y = _text_block(d, left, y, ["The Daily Chronicle"], _font(92), 92, DAILY_INK, width=CONTENT_W, bold=1)
    d.rectangle([left, y, right, y + 4], fill=DAILY_INK)
    d.line([(left, y + 10), (right, y + 10)], fill=DAILY_INK, width=1)
    y += 20
    y = _text_block(d, left, y, [_today()], _font(22), 22, DAILY_INK, width=CONTENT_W)
    d.line([(left, y), (right, y)], fill=DAILY_INK, width=1)
    y += 28
    if story.kicker:
        font, size, lines, cut_kicker = _fit(story.kicker.upper(), 24, 18, CONTENT_W, 1)
        y = _text_block(d, left, y, lines, font, size, GREY, width=CONTENT_W)
        if cut_kicker:
            cut["kicker"] = cut_kicker
    font, size, lines, cut_headline = _fit(story.headline, 64, 40, CONTENT_W, 3)
    y = _text_block(d, left, y, lines, font, size, DAILY_INK, width=CONTENT_W, bold=1) + 4
    if cut_headline:
        cut["headline"] = cut_headline
    if story.byline:
        font, size, lines, cut_byline = _fit(story.byline, 22, 18, CONTENT_W, 1)
        y = _text_block(d, left, y, lines, font, size, GREY, width=CONTENT_W)
        if cut_byline:
            cut["byline"] = cut_byline
    y += 16
    if story.caption:
        cap_font, cap_size, cap_lines, cut_caption = _fit(story.caption, 20, 18, CONTENT_W, 2)
        if cut_caption:
            cut["caption"] = cut_caption
    else:
        cap_font, cap_size, cap_lines = None, 0, []
    # A short story leaves paper to spare: the photo grows into it (up to a 4:3 crop of a wide photo),
    # and a story that is still short sets its body type larger, so the page ends near the bottom rule.
    max_w = CONTENT_W - 24
    room = BOTTOM - 16 - (y + 12 + 24 + len(cap_lines) * _lead(cap_size) + 14 + 20)
    grow_to = min(DAILY_PHOTO_MAX, round(max_w / min(photo.width / photo.height, 4 / 3)))
    need = _columns_height(story.story, CONTENT_W, 3, 34, DAILY_BODY)
    height = max(DAILY_PHOTO, min(grow_to, room - need))
    px0, py0, px1, py1 = _photo_box(photo, y + 12, height, max_w)
    _photo(page, photo, (px0, py0, px1, py1), mono=True)
    d.rectangle([px0 - 12, y, px1 + 12, py1 + 12], outline=DAILY_INK, width=2)  # thin frame with a white mat
    y = py1 + 24
    if cap_lines:
        y = _text_block(d, left, y, cap_lines, cap_font, cap_size, GREY, width=CONTENT_W)
    y += 14
    d.line([(left, y), (right, y)], fill=DAILY_INK, width=1)
    d.line([(left, BOTTOM), (right, BOTTOM)], fill=DAILY_INK, width=1)
    story_cut = _columns(d, story.story, (left, y + 20, right, BOTTOM - 16), 3, 34, DAILY_BODY_MAX, 14, DAILY_INK,
                         rule=GREY)
    if story_cut:
        cut["story"] = story_cut
    return cut


# --- themed styles ---------------------------------------------------------------------------
# The page font has no star or ornament glyphs, so every decoration is drawn from shapes.


def _star(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill, points: int = 5,
          inner: float = 0.45) -> None:
    pts = []
    for i in range(points * 2):
        radius = r if i % 2 == 0 else r * inner
        angle = math.radians(-90 + i * 180 / points)
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    d.polygon(pts, fill=fill)


def _moon(page: Image.Image, cx: int, cy: int, r: int, fill) -> None:
    """A crescent: a disc with an offset disc taken out of it."""
    mask = Image.new("L", (2 * r + 1, 2 * r + 1), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([0, 0, 2 * r, 2 * r], fill=255)
    md.ellipse([round(r * 0.6), round(-r * 0.3), round(r * 2.6), round(r * 1.7)], fill=0)
    page.paste(fill, (cx - r, cy - r, cx + r + 1, cy + r + 1), mask)


def _arch(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int], rise: int, fill) -> None:
    """A window with a rounded top, the shape of the wizard card's portrait frame."""
    x0, y0, x1, y1 = box
    d.ellipse([x0, y0, x1, y0 + 2 * rise], fill=fill)
    d.rectangle([x0, y0 + rise, x1, y1], fill=fill)


def _grow(box: tuple[int, int, int, int], by: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    return x0 - by, y0 - by, x1 + by, y1 + by


def _centred(d: ImageDraw.ImageDraw, y: int, text: str, size: int, fill, bold: int = 0,
             min_size: int | None = None, width: int = CONTENT_W - 120, lines: int = 1):
    """Fit ``text`` (shrinking to ``min_size``) in ``lines`` centred lines; returns (y below, cut)."""
    font, size, wrapped, cut = _fit(text, size, min_size or size, width, lines)
    return _text_block(d, (W - width) // 2, y, wrapped, font, size, fill, width=width, bold=bold), cut


NAVY = (20, 22, 58)
PLUM = (62, 30, 94)
GOLD = (212, 170, 60)
GOLD_LIGHT = (240, 214, 130)
GOLD_DARK = (140, 100, 28)
CREAM = (247, 238, 212)
GUIDE = (170, 170, 170)
CARD_W, CARD_H = 1000, 1397  # a trading card's 63 × 88 proportions, as large as the page allows
WIZARD_WINDOW, WIZARD_RISE = 640, 150  # portrait window height, and how high its arch rises


def _wizard(page: Image.Image, photo: Image.Image, story: Story) -> Cuts:
    """Trading card: dark card with a gold double border, arched portrait, emblem, name banner, lore box."""
    d = ImageDraw.Draw(page)
    cut: Cuts = {}
    x0, y0 = (W - CARD_W) // 2, (H - CARD_H) // 2
    x1, y1 = x0 + CARD_W, y0 + CARD_H
    mid = W // 2
    for x, y, sx, sy in ((x0, y0, -1, -1), (x1, y0, 1, -1), (x0, y1, -1, 1), (x1, y1, 1, 1)):  # cut guides
        d.line([(x + sx * 6, y), (x + sx * 24, y)], fill=GUIDE, width=2)
        d.line([(x, y + sy * 6), (x, y + sy * 24)], fill=GUIDE, width=2)
    card_size = (CARD_W, CARD_H)
    card = Image.composite(Image.new("RGB", card_size, PLUM), Image.new("RGB", card_size, NAVY),
                           Image.linear_gradient("L").resize(card_size))
    sparkle = ImageDraw.Draw(card)
    rng = random.Random(63)  # fixed, so the same card prints the same every time
    for _ in range(70):  # only around the portrait, clear of the text
        sx, sy, r = rng.randrange(50, CARD_W - 50), rng.randrange(110, 820), rng.choice((1, 1, 2, 3))
        sparkle.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(150, 140, 205))
    shape = Image.new("L", card_size, 0)
    ImageDraw.Draw(shape).rounded_rectangle([0, 0, CARD_W - 1, CARD_H - 1], radius=36, fill=255)
    page.paste(card, (x0, y0), shape)
    d.rounded_rectangle([x0 + 14, y0 + 14, x1 - 14, y1 - 14], radius=26, outline=GOLD, width=8)
    d.rounded_rectangle([x0 + 30, y0 + 30, x1 - 30, y1 - 30], radius=16, outline=GOLD, width=2)
    for cx, cy in ((x0 + 30, y0 + 30), (x1 - 30, y0 + 30), (x0 + 30, y1 - 30), (x1 - 30, y1 - 30)):
        _star(d, cx, cy, 14, GOLD, points=4, inner=0.35)

    if story.kicker.strip():
        font, size, [label], cut_kicker = _fit(story.kicker.upper(), 30, 20, 780, 1)
        y = y0 + 54
        d.text((mid - font.getlength(label) / 2, y), label, font=font, fill=GOLD_LIGHT)
        for side in (-1, 1):
            _star(d, mid + side * (font.getlength(label) / 2 + 26), y + size * 0.62, 10, GOLD, points=4,
                  inner=0.35)
        if cut_kicker:
            cut["kicker"] = cut_kicker

    box = _photo_box(photo, y0 + 118, WIZARD_WINDOW, 800)
    rise = WIZARD_RISE
    for grow, colour in ((20, GOLD), (12, NAVY), (6, GOLD)):  # double gold frame following the arch
        _arch(d, _grow(box, grow), rise + grow, colour)
    frame = page.crop(box)
    _photo(page, photo, box, mono=False)
    window = Image.new("L", (box[2] - box[0], box[3] - box[1]), 255)
    _arch(ImageDraw.Draw(window), (0, 0, window.width - 1, window.height - 1), rise, 0)
    page.paste(frame, box[:2], window)  # put the frame back over the corners above the arch

    ey = box[3] + 6  # emblem: a moon and a star in a gold medallion, over the foot of the frame
    d.ellipse([mid - 58, ey - 58, mid + 58, ey + 58], fill=GOLD)
    d.ellipse([mid - 48, ey - 48, mid + 48, ey + 48], fill=NAVY)
    _moon(page, mid - 8, ey, 30, GOLD)
    _star(d, mid + 20, ey - 14, 13, GOLD_LIGHT)

    by = ey + 58 + 20  # name banner: a gold ribbon with notched tails
    font, size, lines, cut_name = _fit(story.headline, 60, 40, 720, 1)
    if cut_name:
        font, size, lines, cut_name = _fit(story.headline, 40, 40, 720, 2)
    if cut_name:
        cut["headline"] = cut_name
    bh = len(lines) * _lead(size) + 24
    for side in (-1, 1):
        inner_x, outer_x = mid + side * 380, mid + side * 450
        notch = outer_x - side * 28
        d.polygon([(inner_x, by + 22), (outer_x, by + 22), (notch, by + 22 + bh / 2), (outer_x, by + 22 + bh),
                   (inner_x, by + 22 + bh)], fill=GOLD_DARK)
    d.rectangle([mid - 410, by, mid + 410, by + bh], fill=GOLD, outline=GOLD_DARK, width=3)
    _text_block(d, mid - 360, by + 12, lines, font, size, NAVY, width=720, bold=1)
    y = by + bh + 22 + 10

    if story.caption:
        y, cut_caption = _centred(d, y, story.caption, 30, GOLD_LIGHT, min_size=22, width=760, lines=2)
        if cut_caption:
            cut["caption"] = cut_caption
    lore = (x0 + 70, y + 10, x1 - 70, y1 - 96)
    d.rounded_rectangle(lore, radius=14, fill=CREAM, outline=GOLD, width=3)
    story_cut = _columns(d, story.story, (lore[0] + 30, lore[1] + 20, lore[2] - 30, lore[3] - 14), 1, 0, 28, 16,
                         NAVY, centre=True)
    if story_cut:
        cut["story"] = story_cut
    if story.byline:
        _, cut_byline = _centred(d, y1 - 80, story.byline, 22, GOLD_LIGHT, min_size=18, width=640)
        if cut_byline:
            cut["byline"] = cut_byline
    return cut


PARCHMENT = (236, 216, 170)
SEPIA = (88, 56, 30)
SEPIA_DARK = (58, 34, 16)
RUST = (130, 42, 24)
WANTED_PHOTO, WANTED_PHOTO_MAX = 560, 700  # photo height: normal, and grown into space a short text leaves


@lru_cache(maxsize=1)
def _parchment() -> tuple[Image.Image, Image.Image]:
    """Aged paper the size of the printable area, and a mask with torn edges (made once, seeded)."""
    w, h = CONTENT_W, BOTTOM - MARGIN
    rng = random.Random(1848)
    paper = Image.new("RGB", (w, h), PARCHMENT)
    edges = Image.radial_gradient("L").resize((w, h)).point(lambda v: min(255, max(0, v - 110) * 2))
    paper = Image.composite(Image.new("RGB", (w, h), (176, 128, 72)), paper, edges)
    for cell, colour, depth in ((48, (196, 158, 104), 0.55), (6, (150, 110, 64), 0.18)):  # stains, then grain
        cw, ch = w // cell + 2, h // cell + 2
        noise = Image.frombytes("L", (cw, ch), rng.randbytes(cw * ch)).resize((w, h), Image.Resampling.BICUBIC)
        paper = Image.composite(Image.new("RGB", (w, h), colour), paper,
                                noise.point(lambda v, k=depth: round(max(0, v - 110) * k)))
    outline = []
    for i in range(0, w - 12, 24):  # walk round the edge, tearing a little off as we go
        outline.append((i, rng.randint(0, 10)))
    for i in range(12, h - 12, 24):
        outline.append((w - 1 - rng.randint(0, 10), i))
    for i in range(w - 13, 12, -24):
        outline.append((i, h - 1 - rng.randint(0, 10)))
    for i in range(h - 13, 12, -24):
        outline.append((rng.randint(0, 10), i))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).polygon(outline, fill=255)
    ImageDraw.Draw(paper).line(outline + outline[:1], fill=(150, 104, 56), width=3)
    return paper, mask


def _wanted(page: Image.Image, photo: Image.Image, story: Story) -> Cuts:
    """Wanted poster: torn parchment, huge WANTED, framed sepia-tinted photo, name, reward, description."""
    d = ImageDraw.Draw(page)
    cut: Cuts = {}
    mid = W // 2
    paper, mask = _parchment()
    page.paste(paper, (MARGIN, MARGIN), mask)
    d.ellipse([mid - 11, MARGIN + 18, mid + 11, MARGIN + 40], fill=(105, 96, 86))  # the nail it hangs on
    d.ellipse([mid - 6, MARGIN + 22, mid + 1, MARGIN + 29], fill=(196, 186, 170))

    size = next(s for s in range(240, 100, -4) if _font(s).getlength("WANTED") <= CONTENT_W - 180)
    font = _font(size)
    top, bottom = font.getbbox("WANTED")[1::2]
    d.text((mid - font.getlength("WANTED") / 2, MARGIN + 74 - top), "WANTED", font=font, fill=SEPIA_DARK,
           stroke_width=3, stroke_fill=SEPIA_DARK)
    y = MARGIN + 74 + bottom - top + 28
    d.line([(MARGIN + 70, y), (W - MARGIN - 70, y)], fill=SEPIA_DARK, width=5)
    d.line([(MARGIN + 70, y + 11), (W - MARGIN - 70, y + 11)], fill=SEPIA_DARK, width=2)
    y += 11

    text_w = CONTENT_W - 120
    if story.kicker.strip():
        k_font, k_size, [label], cut_kicker = _fit(story.kicker.upper(), 60, 24, text_w, 1)
    name = _fit(story.headline.upper(), 104, 60, text_w, 1)
    if name[3]:  # too long for one line even at the smallest size: allow a second
        name = _fit(story.headline.upper(), 60, 60, text_w, 2)
    reward = None
    if story.caption:
        reward = _fit(story.caption.upper(), 64, 40, text_w, 1)
        if reward[3]:
            reward = _fit(story.caption.upper(), 40, 40, text_w, 2)
    # The photo grows into whatever a short description leaves (up to a 4:3 crop of a wide photo).
    words = (_lead(k_size) + 6 if story.kicker.strip() else 0) + len(name[2]) * _lead(name[1]) + \
        (4 + len(reward[2]) * _lead(reward[1]) if reward else 0)
    room = BOTTOM - 104 - (y + 50 + 22 + 26 + words + 34)
    max_w = CONTENT_W - 200
    grow_to = min(WANTED_PHOTO_MAX, round(max_w / min(photo.width / photo.height, 4 / 3)))
    height = max(WANTED_PHOTO, min(grow_to, room - _columns_height(story.story, CONTENT_W - 200, 1, 0, 30)))

    box = _photo_box(photo, y + 50, height, max_w)
    d.rectangle(_grow(box, 22), fill=SEPIA_DARK)  # heavy frame with a pale inner line
    d.rectangle(_grow(box, 10), outline=(214, 186, 136), width=2)
    _photo(page, photo, box, mono=False)
    shot = page.crop(box)  # tone the photo towards the paper, keeping its colours
    sepia = ImageOps.colorize(ImageOps.grayscale(shot), black=(40, 24, 10), white=(250, 236, 200))
    page.paste(Image.blend(shot, sepia, 0.35), box[:2])
    y = box[3] + 22 + 26

    if story.kicker.strip():
        d.text((mid - k_font.getlength(label) / 2, y), label, font=k_font, fill=SEPIA_DARK, stroke_width=1,
               stroke_fill=SEPIA_DARK)
        half = k_font.getlength(label) / 2 + 26
        rule_y = y + k_size * 0.62
        if half + 40 < CONTENT_W / 2 - 70:
            for side in (-1, 1):
                d.line([(mid + side * half, rule_y), (mid + side * (CONTENT_W / 2 - 70), rule_y)],
                       fill=SEPIA_DARK, width=3)
        y += _lead(k_size) + 6
        if cut_kicker:
            cut["kicker"] = cut_kicker
    font, size, lines, cut_name = name
    y = _text_block(d, MARGIN + 60, y, lines, font, size, SEPIA_DARK, width=text_w, bold=2)
    if cut_name:
        cut["headline"] = cut_name
    if reward:
        font, size, lines, cut_reward = reward
        y = _text_block(d, MARGIN + 60, y + 4, lines, font, size, RUST, width=text_w, bold=1)
        if cut_reward:
            cut["caption"] = cut_reward
    d.line([(mid - 160, y + 14), (mid + 160, y + 14)], fill=SEPIA, width=2)
    story_cut = _columns(d, story.story, (MARGIN + 100, y + 34, W - MARGIN - 100, BOTTOM - 104), 1, 0, 30, 16,
                         SEPIA, centre=True)
    if story_cut:
        cut["story"] = story_cut
    if story.byline:
        _, cut_byline = _centred(d, BOTTOM - 66, story.byline, 24, SEPIA, min_size=18, width=760)
        if cut_byline:
            cut["byline"] = cut_byline
    return cut


def _ring(page: Image.Image, photo: Image.Image, box: tuple[int, int, int, int],
           rings: tuple[tuple[int, tuple[int, int, int]], ...]) -> None:
    """The photo in an oval window, inside rings drawn ``grow`` pixels out from it, from the outside in."""
    d = ImageDraw.Draw(page)
    for grow, colour in rings:
        d.ellipse(_grow(box, grow), fill=colour)
    frame = page.crop(box)
    _photo(page, photo, box, mono=False)
    window = Image.new("L", (box[2] - box[0], box[3] - box[1]), 255)
    ImageDraw.Draw(window).ellipse([0, 0, window.width - 1, window.height - 1], fill=0)
    page.paste(frame, box[:2], window)  # put the rings back over the corners outside the oval


def _seal(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill, dark, ribbon) -> None:
    """A gold seal: two ribbon tails under a notched disc with a star in the middle."""
    for side in (-1, 1):
        x = cx + side * r * 0.45
        tail_y = cy + r * 1.55
        d.polygon([(x - r * 0.28, cy), (x + r * 0.28, cy), (x + r * 0.28 + side * r * 0.2, tail_y),
                   (x + side * r * 0.1, tail_y - r * 0.25), (x - r * 0.28 + side * r * 0.2, tail_y)], fill=ribbon)
    _star(d, cx, cy, r, fill, points=24, inner=0.88)
    d.ellipse([cx - r * 0.74, cy - r * 0.74, cx + r * 0.74, cy + r * 0.74], outline=dark, width=3)
    _star(d, cx, cy, r * 0.5, dark)


IVORY = (251, 247, 234)
LAUREL = (22, 72, 56)
CERT_PHOTO = 400  # height of the oval portrait


def _certificate(page: Image.Image, photo: Image.Image, story: Story) -> Cuts:
    """Certificate: ivory paper in a green-and-gold border, title, oval portrait, name, award, citation, seal."""
    d = ImageDraw.Draw(page)
    cut: Cuts = {}
    mid = W // 2
    left, top, right, bottom = MARGIN, MARGIN, W - MARGIN, BOTTOM
    d.rectangle([left, top, right, bottom], fill=IVORY)
    d.rectangle([left + 16, top + 16, right - 16, bottom - 16], outline=LAUREL, width=14)
    d.rectangle([left + 40, top + 40, right - 40, bottom - 40], outline=GOLD, width=4)
    d.rectangle([left + 50, top + 50, right - 50, bottom - 50], outline=GOLD, width=1)
    for cx, cy in ((left + 40, top + 40), (right - 40, top + 40), (left + 40, bottom - 40), (right - 40, bottom - 40)):
        d.polygon([(cx, cy - 18), (cx + 18, cy), (cx, cy + 18), (cx - 18, cy)], fill=GOLD)
        d.polygon([(cx, cy - 8), (cx + 8, cy), (cx, cy + 8), (cx - 8, cy)], fill=LAUREL)
    text_w = CONTENT_W - 180

    y = top + 96
    if story.kicker.strip():
        y, cut_kicker = _centred(d, y, story.kicker.upper(), 52, LAUREL, bold=1, min_size=22, width=text_w)
        if cut_kicker:
            cut["kicker"] = cut_kicker
    y += 10
    d.line([(mid - 220, y), (mid - 24, y)], fill=GOLD, width=3)
    d.line([(mid + 24, y), (mid + 220, y)], fill=GOLD, width=3)
    d.polygon([(mid, y - 10), (mid + 10, y), (mid, y + 10), (mid - 10, y)], fill=GOLD)
    y += 44

    width = round(CERT_PHOTO * 0.82)
    box = (mid - width // 2, y, mid + width // 2, y + CERT_PHOTO)
    _ring(page, photo, box, ((16, GOLD), (10, IVORY), (5, GOLD_DARK)))
    y = box[3] + 16 + 30

    y = _text_block(d, (W - text_w) // 2, y, ["This certificate is proudly presented to"], _font(26), 26, GREY,
                    width=text_w) + 6
    name = _fit(story.headline, 80, 48, text_w, 1)
    if name[3]:  # too long for one line even at the smallest size: allow a second
        name = _fit(story.headline, 48, 48, text_w, 2)
    font, size, lines, cut_name = name
    y = _text_block(d, (W - text_w) // 2, y, lines, font, size, INK, width=text_w, bold=1)
    if cut_name:
        cut["headline"] = cut_name
    d.line([(mid - 300, y + 8), (mid + 300, y + 8)], fill=GOLD, width=3)
    y += 28
    if story.caption.strip():
        y, cut_caption = _centred(d, y, story.caption, 36, LAUREL, bold=1, min_size=24, width=text_w, lines=2)
        if cut_caption:
            cut["caption"] = cut_caption
    y += 18

    seal_y, line_y = bottom - 190, bottom - 150  # the seal between the date and signature lines
    citation = (left + 130, y, right - 130, seal_y - 84 - 24)
    spare = citation[3] - citation[1] - _columns_height(story.story, citation[2] - citation[0], 1, 0, 28)
    if spare > 0:  # a short citation sits in the middle of the space it has
        citation = (citation[0], citation[1] + spare // 2, citation[2], citation[3])
    if story_cut := _columns(d, story.story, citation, 1, 0, 28, 16, INK, centre=True):
        cut["story"] = story_cut

    _seal(d, mid, seal_y, 84, GOLD, GOLD_DARK, LAUREL)
    small = _font(18)
    for x0, x1, label in ((left + 90, left + 410, "DATE"), (right - 410, right - 90, "SIGNED")):
        d.line([(x0, line_y), (x1, line_y)], fill=INK, width=2)
        d.text(((x0 + x1 - small.getlength(label)) / 2, line_y + 10), label, font=small, fill=GREY)
    date_font = _fit(_today(), 24, 16, 320, 1)
    _text_block(d, left + 90, line_y - 8 - _lead(date_font[1]), date_font[2], date_font[0], date_font[1], INK,
                width=320)
    if story.byline.strip():
        font, size, lines, cut_byline = _fit(story.byline, 26, 18, 320, 2)
        _text_block(d, right - 410, line_y - 8 - len(lines) * _lead(size), lines, font, size, INK, width=320)
        if cut_byline:
            cut["byline"] = cut_byline
    return cut


ROYAL = (24, 62, 158)
ROYAL_DARK = (14, 36, 98)
SCARLET = (214, 36, 44)
YELLOW = (252, 196, 28)
SPORTS_PHOTO = 720  # height of the photo on the card


def _sports(page: Image.Image, photo: Image.Image, story: Story) -> Cuts:
    """Sports trading card: white card stock, royal-blue panel with stripes, full-width photo, slanted name bar."""
    d = ImageDraw.Draw(page)
    cut: Cuts = {}
    x0, y0 = (W - CARD_W) // 2, (H - CARD_H) // 2
    x1, y1 = x0 + CARD_W, y0 + CARD_H
    mid = W // 2
    for x, y, sx, sy in ((x0, y0, -1, -1), (x1, y0, 1, -1), (x0, y1, -1, 1), (x1, y1, 1, 1)):  # cut guides
        d.line([(x + sx * 6, y), (x + sx * 24, y)], fill=GUIDE, width=2)
        d.line([(x, y + sy * 6), (x, y + sy * 24)], fill=GUIDE, width=2)
    d.rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=30, fill=WHITE, outline=GUIDE, width=2)
    panel = (x0 + 28, y0 + 28, x1 - 28, y1 - 28)
    card_size = (panel[2] - panel[0], panel[3] - panel[1])
    card = Image.composite(Image.new("RGB", card_size, ROYAL_DARK), Image.new("RGB", card_size, ROYAL),
                           Image.linear_gradient("L").resize(card_size))
    stripes = ImageDraw.Draw(card)
    for offset, colour, wide in ((0, SCARLET, 70), (96, YELLOW, 18), (130, WHITE, 8)):  # across the top corner
        a = card_size[0] - 380 + offset
        stripes.polygon([(a, 0), (a + wide, 0), (a + wide + 300, 300), (a + 300, 300)], fill=colour)
    shape = Image.new("L", card_size, 0)
    ImageDraw.Draw(shape).rounded_rectangle([0, 0, card_size[0] - 1, card_size[1] - 1], radius=18, fill=255)
    page.paste(card, panel[:2], shape)

    if story.kicker.strip():
        font, size, [label], cut_kicker = _fit(story.kicker.upper(), 44, 20, 780, 1)
        d.text((x0 + 70, y0 + 52 + (44 - size) // 2), label, font=font, fill=WHITE, stroke_width=1, stroke_fill=WHITE)
        if cut_kicker:
            cut["kicker"] = cut_kicker

    box = (x0 + 64, y0 + 120, x1 - 64, y0 + 120 + SPORTS_PHOTO)
    d.rectangle(_grow(box, 10), fill=WHITE)
    _photo(page, photo, box, mono=False)
    d.rectangle(_grow(box, 2), outline=ROYAL_DARK, width=2)
    bx, by = box[2] - 40, box[1] + 40  # a star badge on the photo's top corner
    d.ellipse([bx - 52, by - 52, bx + 52, by + 52], fill=YELLOW, outline=WHITE, width=6)
    _star(d, bx, by, 34, SCARLET)

    name = _fit(story.headline.upper(), 64, 36, 760, 1)
    if name[3]:  # too long for one line even at the smallest size: allow a second
        name = _fit(story.headline.upper(), 36, 36, 760, 2)
    font, size, lines, cut_name = name
    if cut_name:
        cut["headline"] = cut_name
    top = box[3] - 48
    bar = len(lines) * _lead(size) + 24
    slant = 40
    d.polygon([(x0 + 40 + slant, top + 8), (x1 - 40 + 8, top + 8), (x1 - 40 - slant + 8, top + bar + 8),
               (x0 + 40 + 8, top + bar + 8)], fill=ROYAL_DARK)  # shadow
    d.polygon([(x0 + 40 + slant, top), (x1 - 40, top), (x1 - 40 - slant, top + bar), (x0 + 40, top + bar)],
              fill=SCARLET, outline=WHITE, width=4)
    _text_block(d, mid - 380, top + 12, lines, font, size, WHITE, width=760, bold=2)
    y = top + bar + 22

    if story.caption.strip():
        y, cut_caption = _centred(d, y, story.caption.upper(), 34, YELLOW, bold=1, min_size=22, width=800)
        if cut_caption:
            cut["caption"] = cut_caption
    stats = (x0 + 70, y + 8, x1 - 70, y1 - 96)
    d.rounded_rectangle(stats, radius=12, fill=WHITE, outline=YELLOW, width=4)
    story_cut = _columns(d, story.story, (stats[0] + 28, stats[1] + 18, stats[2] - 28, stats[3] - 12), 1, 0, 26, 14,
                         ROYAL_DARK)
    if story_cut:
        cut["story"] = story_cut
    if story.byline.strip():
        _, cut_byline = _centred(d, y1 - 82, story.byline.upper(), 22, WHITE, min_size=16, width=800)
        if cut_byline:
            cut["byline"] = cut_byline
    return cut


MISSING_RED = (200, 16, 30)
MISSING_PHOTO, MISSING_PHOTO_MAX = 520, 680  # photo height: normal, and grown into space a short text leaves
TABS, TAB_H = 8, 230  # tear-off strips along the foot of the poster, each carrying the byline


def _missing(page: Image.Image, photo: Image.Image, story: Story) -> Cuts:
    """Missing-pet poster: huge red MISSING, the question, a framed photo, name, reward, description, tear-offs."""
    d = ImageDraw.Draw(page)
    cut: Cuts = {}
    mid = W // 2
    left, right = MARGIN, W - MARGIN
    text_w = CONTENT_W - 80
    d.rectangle([left, MARGIN, right, MARGIN + 18], fill=MISSING_RED)

    size = next(s for s in range(240, 100, -4) if _font(s).getlength("MISSING") <= CONTENT_W - 120)
    font = _font(size)
    top, bottom = font.getbbox("MISSING")[1::2]
    d.text((mid - font.getlength("MISSING") / 2, MARGIN + 48 - top), "MISSING", font=font, fill=MISSING_RED,
           stroke_width=4, stroke_fill=MISSING_RED)
    y = MARGIN + 48 + bottom - top + 26
    if story.kicker.strip():
        y, cut_kicker = _centred(d, y, story.kicker.upper(), 56, INK, bold=2, min_size=24, width=text_w)
        if cut_kicker:
            cut["kicker"] = cut_kicker

    name = _fit(story.headline.upper(), 88, 52, text_w, 1)
    if name[3]:  # too long for one line even at the smallest size: allow a second
        name = _fit(story.headline.upper(), 52, 52, text_w, 2)
    reward = None
    if story.caption.strip():
        reward = _fit(story.caption.upper(), 44, 28, text_w, 2)
    tabs_top = BOTTOM - TAB_H if story.byline.strip() else BOTTOM
    # The photo grows into whatever a short description leaves (up to a 4:3 crop of a wide photo).
    words = len(name[2]) * _lead(name[1]) + (6 + len(reward[2]) * _lead(reward[1]) if reward else 0)
    room = tabs_top - 30 - (y + 24 + 24 + 22 + words + 26)
    max_w = CONTENT_W - 220
    grow_to = min(MISSING_PHOTO_MAX, round(max_w / min(photo.width / photo.height, 4 / 3)))
    height = max(MISSING_PHOTO, min(grow_to, room - _columns_height(story.story, text_w - 40, 1, 0, 30)))
    box = _photo_box(photo, y + 24, height, max_w)
    d.rectangle(_grow(box, 8), outline=INK, width=4)
    _photo(page, photo, box, mono=False)
    y = box[3] + 8 + 22

    font, size, lines, cut_name = name
    y = _text_block(d, (W - text_w) // 2, y, lines, font, size, INK, width=text_w, bold=2)
    if cut_name:
        cut["headline"] = cut_name
    if reward:
        font, size, lines, cut_reward = reward
        y = _text_block(d, (W - text_w) // 2, y + 6, lines, font, size, MISSING_RED, width=text_w, bold=1)
        if cut_reward:
            cut["caption"] = cut_reward
    if story_cut := _columns(d, story.story, (left + 60, y + 26, right - 60, tabs_top - 30), 1, 0, 30, 16, INK,
                             centre=True):
        cut["story"] = story_cut

    if story.byline.strip():
        tab_w = CONTENT_W // TABS
        font, size, lines, cut_byline = _fit(story.byline, 24, 16, TAB_H - 36, 2)
        if cut_byline:
            cut["byline"] = cut_byline
        strip = Image.new("RGB", (TAB_H - 20, tab_w), WHITE)  # one tab's text, drawn level and turned upright
        sd = ImageDraw.Draw(strip)
        _text_block(sd, 8, (tab_w - len(lines) * _lead(size)) // 2, lines, font, size, INK, width=TAB_H - 36)
        strip = strip.rotate(90, expand=True)
        for i in range(TABS):
            page.paste(strip, (left + i * tab_w, tabs_top + 10))
        for x in range(left, right, 24):  # the dashed line to tear along, and between the tabs
            d.line([(x, tabs_top), (min(x + 12, right), tabs_top)], fill=INK, width=2)
        for i in range(1, TABS):
            x = left + i * tab_w
            for ty in range(tabs_top, BOTTOM, 24):
                d.line([(x, ty), (x, min(ty + 12, BOTTOM))], fill=INK, width=2)
    return cut


SCREEN = (10, 10, 16)  # the poster's near-black
MARQUEE = (246, 192, 74)
SILVER = (205, 205, 212)
MOVIE_PHOTO, MOVIE_PHOTO_MAX = 760, 1000  # photo height: normal, and grown into space short credits leave


def _movie(page: Image.Image, photo: Image.Image, story: Story) -> Cuts:
    """Movie poster: a dark poster, tagline, the photo fading into the dark, huge title, stars, release line,
    the credits block and a rating box."""
    d = ImageDraw.Draw(page)
    cut: Cuts = {}
    mid = W // 2
    left, top, right, bottom = MARGIN, MARGIN, W - MARGIN, BOTTOM
    text_w = CONTENT_W - 120
    d.rectangle([left, top, right, bottom], fill=SCREEN)

    title = _fit(story.headline.upper(), 120, 72, text_w, 1)
    if title[3]:  # too long for one line even at the smallest size: allow a second, smaller
        title = _fit(story.headline.upper(), 72, 48, text_w, 2)
    release = _fit(story.caption.upper(), 40, 24, text_w, 2) if story.caption.strip() else None
    rating = _fit(story.byline, 22, 16, 760, 2) if story.byline.strip() else None
    title_h = len(title[2]) * _lead(title[1])
    foot = bottom - 30 - (len(rating[2]) * _lead(rating[1]) + 18 + 24 if rating else 0)
    # The photo grows into whatever short credits leave; the title sits across its faded foot.
    below = title_h - title_h // 2 - 30 + 70 + (len(release[2]) * _lead(release[1]) if release else 0) + 24 + \
        _columns_height(story.story, CONTENT_W - 160, 1, 0, 22)
    height = max(MOVIE_PHOTO, min(MOVIE_PHOTO_MAX, foot - below - (top + 96)))
    box = (left, top + 96, right, top + 96 + height)
    _photo(page, photo, box, mono=False)
    fade = Image.linear_gradient("L")  # black at the top, white at the foot
    width = box[2] - box[0]
    page.paste(SCREEN, (box[0], box[3] - 380, box[2], box[3]), fade.resize((width, 380)))
    page.paste(SCREEN, (box[0], box[1], box[2], box[1] + 60), fade.rotate(180).resize((width, 60)))
    if story.kicker.strip():
        _, cut_kicker = _centred(d, top + 34, story.kicker.upper(), 30, SILVER, min_size=20, width=text_w)
        if cut_kicker:
            cut["kicker"] = cut_kicker

    font, size, lines, cut_title = title
    y = _text_block(d, (W - text_w) // 2, box[3] - 30 - title_h // 2, lines, font, size, MARQUEE, width=text_w,
                    bold=3)
    if cut_title:
        cut["headline"] = cut_title
    for i in range(5):  # the critics' stars
        _star(d, mid + (i - 2) * 52, y + 26, 20, MARQUEE)
    y += 70
    if release:
        font, size, lines, cut_release = release
        y = _text_block(d, (W - text_w) // 2, y, lines, font, size, WHITE, width=text_w, bold=1)
        if cut_release:
            cut["caption"] = cut_release

    if rating:  # the rating box at the foot of the poster
        font, size, lines, _ = rating
        box_w = max(font.getlength(line) for line in lines) + 40
        box_h = len(lines) * _lead(size) + 18
        d.rectangle([mid - box_w / 2, foot + 24, mid + box_w / 2, foot + 24 + box_h], outline=SILVER, width=2)
        _text_block(d, mid - 380, foot + 34, lines, font, size, SILVER, width=760)
    credits = (left + 80, y + 24, right - 80, foot)
    spare = credits[3] - credits[1] - _columns_height(story.story, credits[2] - credits[0], 1, 0, 22)
    if spare > 0:  # the credits sit at the foot of the poster, as on a real one
        credits = (credits[0], credits[1] + spare, credits[2], credits[3])
    if story_cut := _columns(d, story.story, credits, 1, 0, 22, 12, SILVER, centre=True):
        cut["story"] = story_cut
    if rating and rating[3]:  # reported after the credits, which are printed above the rating box
        cut["byline"] = rating[3]
    return cut


STYLES: dict[str, Callable[[Image.Image, Image.Image, Story], Cuts]] = {
    "metro": _metro, "daily": _daily, "wizard": _wizard, "wanted": _wanted, "certificate": _certificate,
    "sports": _sports, "missing": _missing, "movie": _movie}


def compose(photo: Image.Image, story: Story | None = None, style: str = "metro") -> tuple[Image.Image, Cuts]:
    """Lay out the page (with the style's default story if none is given); also returns the fields
    (in page order) that were cut with "…" to fit, each with how much of it was printed."""
    story = default_story(style) if story is None else story
    page = Image.new("RGB", (W, H), "white")
    cut = STYLES[style](page, photo, Story(**{name: _printable(getattr(story, name)) for name in FIELDS}))
    # The styles count on the printed text (stand-ins such as "EUR" for "€"); report the characters given.
    # Their .upper() keeps the length of every character the page font draws, so the count holds there too.
    return page, {name: _in_source(c, getattr(story, name), _printable) for name, c in cut.items()}


def to_jpeg(page: Image.Image) -> bytes:
    buf = io.BytesIO()
    page.save(buf, "JPEG", quality=92, dpi=(150, 150))
    return buf.getvalue()


# File types a page can be saved as, keyed by file extension.
SAVE_FORMATS = {".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


def format_for(path: Path) -> str:
    """The file type to save the page as, from the extension of ``path`` (any case)."""
    fmt = SAVE_FORMATS.get(path.suffix.lower())
    if fmt is None:
        reason = f"unknown file type {path.suffix}" if path.suffix else "no file extension"
        raise PrinterError(f"cannot save {path}: {reason}",
                           hint=f"end the file name in one of {', '.join(SAVE_FORMATS)}", exit_code=2)
    return fmt


def to_bytes(page: Image.Image, fmt: str) -> bytes:
    """The page as a ``pdf`` (one A4 page), ``png`` (lossless) or ``jpeg`` file (the bytes that are printed)."""
    if fmt == "jpeg":
        return to_jpeg(page)
    buf = io.BytesIO()
    if fmt == "pdf":
        page.save(buf, "PDF", resolution=150, quality=92)
    elif fmt == "png":
        page.save(buf, "PNG", dpi=(150, 150))
    else:
        raise ValueError(f"unknown page format {fmt!r}")
    return buf.getvalue()

