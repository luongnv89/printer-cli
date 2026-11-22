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
import time
import unicodedata
from dataclasses import dataclass, fields, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from prt.drivers.base import PrinterError
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
# and compose() names the fields that were cut so the command can warn about them.
LIMITS = {"headline": 120, "story": 2500, "kicker": 40, "caption": 200, "byline": 80}
MAX_STORY_FILE = 64 * 1024

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

DEFAULT_STORIES = {"metro": DEFAULT_STORY, "daily": DEFAULT_STORY, "wizard": WIZARD_STORY, "wanted": WANTED_STORY}
# What each style prints, for messages such as the confirmation prompt.
KINDS = {"metro": "metro newspaper page", "daily": "daily newspaper page", "wizard": "wizard card",
         "wanted": "wanted poster"}


def default_story(style: str) -> Story:
    """The story a style prints when none is given."""
    return DEFAULT_STORIES[style]


def _clean(text: str, keep_newlines: bool) -> str:
    """Drop control characters; keep newlines (paragraph breaks, blank lines dropped) only where asked.

    Elsewhere a line break is a word break, so it becomes a space rather than joining the words.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    if not keep_newlines:
        text = text.replace("\n", " ")
    kept = "".join(c for c in text if (c == "\n" and keep_newlines) or unicodedata.category(c)[0] != "C")
    if keep_newlines:
        # A newline already starts a paragraph with a gap, so blank lines between paragraphs add nothing.
        return "\n".join(line for line in (" ".join(raw.split()) for raw in kept.split("\n")) if line)
    return " ".join(kept.split())


# The built-in font (Aileron, via ImageFont.load_default) covers basic Latin only, so accented
# letters are printed without their marks and a few common symbols get ASCII stand-ins.
_STAND_INS = {"–": "-", "—": "-", "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o",
              "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "€": "EUR", "£": "GBP"}


@lru_cache(maxsize=1)
def _missing_glyph() -> bytes:
    return bytes(_font(40).getmask("\U0010fffd"))


def _has_glyph(c: str) -> bool:
    return bytes(_font(40).getmask(c)) != _missing_glyph()


@lru_cache(maxsize=None)
def _glyphs(c: str) -> str | None:
    """What to draw for one character: itself, its unaccented form, a stand-in, or None."""
    if unicodedata.combining(c):
        return ""  # a separate accent mark (decomposed text) is dropped like a composed one
    if c.isspace() or _has_glyph(c):
        return c
    if c in _STAND_INS:
        return _STAND_INS[c]
    base = "".join(b for b in unicodedata.normalize("NFKD", c) if not unicodedata.combining(b))
    return base if base and all(_has_glyph(b) for b in base) else None


def _printable(text: str) -> str:
    return "".join("?" if (g := _glyphs(c)) is None else g for c in text)


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


def load_story(path: Path) -> Story:
    """Read a story JSON file: {"headline", "story", "kicker"?, "caption"?, "byline"?}."""
    if not path.is_file():
        raise PrinterError(f"story file not found: {path}", hint="check the path", exit_code=3)
    if path.stat().st_size > MAX_STORY_FILE:
        raise PrinterError(f"story file {path.name} is larger than {MAX_STORY_FILE // 1024} KiB",
                           hint="a booth story is a few short paragraphs", exit_code=3)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise PrinterError(f"story file {path.name} is not UTF-8 text", hint="save it as UTF-8 JSON",
                           exit_code=3) from exc
    except json.JSONDecodeError as exc:
        raise PrinterError(f"story file {path.name} is not valid JSON: {exc.msg} (line {exc.lineno})",
                           hint='expected {"headline": "...", "story": "..."}', exit_code=3) from exc
    if not isinstance(data, dict):
        raise PrinterError(f"story file {path.name} must hold a JSON object",
                           hint='expected {"headline": "...", "story": "..."}', exit_code=3)
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise PrinterError(f"unknown field(s) in story file: {', '.join(unknown)}",
                           hint="allowed fields: " + ", ".join(FIELDS), exit_code=3)
    missing = [name for name in REQUIRED if name not in data]
    if missing:
        raise PrinterError(f"story file {path.name} is missing {' and '.join(missing)}",
                           hint='expected {"headline": "...", "story": "..."}', exit_code=3)
    wrong = [name for name, value in data.items() if not isinstance(value, str)]
    if wrong:
        raise PrinterError(f"story file fields must be text: {', '.join(sorted(wrong))}",
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

ELLIPSIS = "…"


def _today() -> str:
    now = time.localtime()
    return time.strftime(f"%A, %B {now.tm_mday}, %Y", now)


def _longest(text: str, fits: Callable[[str], bool]) -> int:
    """Length of the longest prefix of ``text`` that ``fits`` (at least 1, so callers always advance).

    Widths only grow with the prefix, so gallop to bracket the answer and then bisect: a few short
    measurements per line, even for a 2500-character token with no spaces.
    """
    lo, hi = 1, 2
    while hi < len(text) and fits(text[:hi]):
        lo, hi = hi, hi * 2
    hi = min(hi, len(text))
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if fits(text[:mid]) else (lo, mid)
    return hi if fits(text[:hi]) else lo


def _wrap(text: str, font, width: int) -> list[str]:
    """Greedy word wrap; words wider than a line are split by character."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if font.getlength(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        while len(word) > 1:
            cut = _longest(word, lambda part: font.getlength(part) <= width)
            if cut == len(word):
                break
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


def _ellipsize(line: str, font, width: int) -> str:
    """End ``line`` with "…" inside ``width``, dropping whole words; only a lone word is cut mid-word."""
    def fits(part: str) -> bool:
        return font.getlength(part.rstrip() + ELLIPSIS) <= width
    if fits(line):
        return line.rstrip() + ELLIPSIS
    words = line.split()
    for n in range(len(words) - 1, 0, -1):
        head = " ".join(words[:n])
        if fits(head):
            return head + ELLIPSIS
    word = words[0] if words else ""
    return word[:_longest(word, fits)] + ELLIPSIS if word and fits(word[:1]) else ELLIPSIS


def _fit(text: str, size: int, min_size: int, width: int, max_lines: int):
    """Largest font (size → min_size) that wraps ``text`` into ``max_lines``; cut with "…" at the minimum.

    Returns (font, size, lines, cut), where ``cut`` says the text had to be shortened.
    """
    for s in range(size, min_size - 1, -2):
        font = _font(s)
        lines = _wrap(text, font, width)
        if len(lines) <= max_lines:
            return font, s, lines, False
    return font, s, lines[:max_lines - 1] + [_ellipsize(lines[max_lines - 1], font, width)], True


def _lead(size: int) -> int:
    return round(size * 1.3)


def _text_block(d: ImageDraw.ImageDraw, x: int, y: int, lines: list[str], font, size: int, fill,
                width: int | None = None, bold: int = 0) -> int:
    """Draw lines left-aligned at x (or centred within ``width``); returns the y below them."""
    for line in lines:
        dx = (width - font.getlength(line)) / 2 if width else 0
        d.text((x + dx, y), line, font=font, fill=fill, stroke_width=bold, stroke_fill=fill)
        y += _lead(size)
    return y


def _col_width(width: int, columns: int, gutter: int) -> int:
    return (width - gutter * (columns - 1)) // columns


def _paragraphs(text: str, font, width: int) -> list[str]:
    """Wrap each paragraph, with one blank line between paragraphs."""
    lines: list[str] = []
    for para in text.split("\n"):
        if para.strip():
            lines += _wrap(para, font, width) + [""]
    return lines[:-1]


SHORT_STORY = 4  # a story of this many lines or fewer stays in the first column


def _rows(lines: int, columns: int) -> int:
    """Rows per column: balanced like a real page, except that a short story is not split up."""
    return lines if lines <= SHORT_STORY else -(-lines // columns)


def _columns_height(text: str, width: int, columns: int, gutter: int, size: int) -> int:
    """Height the text needs in columns at type ``size``."""
    lines = _paragraphs(text, _font(size), _col_width(width, columns, gutter))
    return _rows(len(lines), columns) * _lead(size)


def _columns(d: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], columns: int, gutter: int,
             size: int, min_size: int, fill, rule=None, centre: bool = False) -> bool:
    """Flow paragraphs down ``columns`` columns, shrinking the type until it fits, else cutting with "…".

    Returns True when the text was cut.
    """
    x0, y0, x1, y1 = box
    col_w = _col_width(x1 - x0, columns, gutter)
    for s in range(size, min_size - 1, -1):
        font = _font(s)
        lines = _paragraphs(text, font, col_w)
        per_col = max(1, (y1 - y0) // _lead(s))
        if len(lines) <= per_col * columns:
            break
    capacity = per_col * columns
    cut = len(lines) > capacity
    if cut:
        lines = lines[:capacity]
        while len(lines) > 1 and not lines[-1]:  # end on text, not on a paragraph gap
            lines.pop()
        lines[-1] = _ellipsize(lines[-1], font, col_w)
    rows = min(per_col, _rows(len(lines), columns))
    col = row = 0
    for line in lines:
        if row == rows:
            col, row = col + 1, 0
        if not line and row == 0:
            continue
        dx = (col_w - font.getlength(line)) / 2 if centre else 0
        d.text((x0 + col * (col_w + gutter) + dx, y0 + row * _lead(s)), line, font=font, fill=fill)
        row += 1
    if rule:
        for c in range(1, min(columns, -(-len(lines) // rows) if rows else 1)):  # only between used columns
            rx = x0 + c * (col_w + gutter) - gutter // 2
            d.line([(rx, y0), (rx, y0 + rows * _lead(s))], fill=rule, width=1)
    return cut


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
CONTENT_W = W - 2 * MARGIN
BOTTOM = H - MARGIN


METRO_PHOTO, METRO_PHOTO_MAX = 560, 900  # photo height: normal, and grown into space a short story leaves
METRO_BODY = 28


def _metro(page: Image.Image, photo: Image.Image, story: Story) -> list[str]:
    """Colour tabloid: reversed black masthead, red accents, shouting headline, full-width photo, 2 columns."""
    d = ImageDraw.Draw(page)
    cut = []
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
        cut += ["kicker"] * cut_kicker
    font, size, lines, cut_headline = _fit(story.headline.upper(), 80, 48, CONTENT_W, 2)
    if cut_headline:  # too long for two lines even at the smallest size: allow a third
        font, size, lines, cut_headline = _fit(story.headline.upper(), 48, 48, CONTENT_W, 3)
    y = _text_block(d, left, y, lines, font, size, INK, bold=2) + 14
    cut += ["headline"] * cut_headline
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
        cut += ["caption"] * cut_caption
    y += 26
    if story.byline:
        y = _text_block(d, left, y, by_lines, by_font, by_size, RED, bold=1) + 6
        cut += ["byline"] * cut_byline
    d.rectangle([left, BOTTOM - 8, right, BOTTOM], fill=RED)
    if _columns(d, story.story, (left, y, right, BOTTOM - 24), 2, 44, METRO_BODY, 16, INK):
        cut.append("story")
    return cut


GREY = (95, 95, 95)
DAILY_INK = (25, 25, 25)
DAILY_PHOTO, DAILY_PHOTO_MAX = 460, 820  # photo height: normal, and grown into space a short story leaves
DAILY_BODY, DAILY_BODY_MAX = 24, 28  # body type: normal, and grown when the photo is already at its largest


def _daily(page: Image.Image, photo: Image.Image, story: Story) -> list[str]:
    """Black-and-white broadsheet: centred masthead, double rules, grey photo with a mat, 3 ruled columns."""
    d = ImageDraw.Draw(page)
    cut = []
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
        cut += ["kicker"] * cut_kicker
    font, size, lines, cut_headline = _fit(story.headline, 64, 40, CONTENT_W, 3)
    y = _text_block(d, left, y, lines, font, size, DAILY_INK, width=CONTENT_W, bold=1) + 4
    cut += ["headline"] * cut_headline
    if story.byline:
        font, size, lines, cut_byline = _fit(story.byline, 22, 18, CONTENT_W, 1)
        y = _text_block(d, left, y, lines, font, size, GREY, width=CONTENT_W)
        cut += ["byline"] * cut_byline
    y += 16
    if story.caption:
        cap_font, cap_size, cap_lines, cut_caption = _fit(story.caption, 20, 18, CONTENT_W, 2)
        cut += ["caption"] * cut_caption
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
    if _columns(d, story.story, (left, y + 20, right, BOTTOM - 16), 3, 34, DAILY_BODY_MAX, 14, DAILY_INK,
                rule=GREY):
        cut.append("story")
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


def _wizard(page: Image.Image, photo: Image.Image, story: Story) -> list[str]:
    """Trading card: dark card with a gold double border, arched portrait, emblem, name banner, lore box."""
    d = ImageDraw.Draw(page)
    cut = []
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
        cut += ["kicker"] * cut_kicker

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
    cut += ["headline"] * cut_name
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
        cut += ["caption"] * cut_caption
    lore = (x0 + 70, y + 10, x1 - 70, y1 - 96)
    d.rounded_rectangle(lore, radius=14, fill=CREAM, outline=GOLD, width=3)
    if _columns(d, story.story, (lore[0] + 30, lore[1] + 20, lore[2] - 30, lore[3] - 14), 1, 0, 28, 16,
                NAVY, centre=True):
        cut.append("story")
    if story.byline:
        _, cut_byline = _centred(d, y1 - 80, story.byline, 22, GOLD_LIGHT, min_size=18, width=640)
        cut += ["byline"] * cut_byline
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


def _wanted(page: Image.Image, photo: Image.Image, story: Story) -> list[str]:
    """Wanted poster: torn parchment, huge WANTED, framed sepia-tinted photo, name, reward, description."""
    d = ImageDraw.Draw(page)
    cut = []
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
        cut += ["kicker"] * cut_kicker
    font, size, lines, cut_name = name
    y = _text_block(d, MARGIN + 60, y, lines, font, size, SEPIA_DARK, width=text_w, bold=2)
    cut += ["headline"] * cut_name
    if reward:
        font, size, lines, cut_reward = reward
        y = _text_block(d, MARGIN + 60, y + 4, lines, font, size, RUST, width=text_w, bold=1)
        cut += ["caption"] * cut_reward
    d.line([(mid - 160, y + 14), (mid + 160, y + 14)], fill=SEPIA, width=2)
    if _columns(d, story.story, (MARGIN + 100, y + 34, W - MARGIN - 100, BOTTOM - 104), 1, 0, 30, 16, SEPIA,
                centre=True):
        cut.append("story")
    if story.byline:
        _, cut_byline = _centred(d, BOTTOM - 66, story.byline, 24, SEPIA, min_size=18, width=760)
        cut += ["byline"] * cut_byline
    return cut


STYLES: dict[str, Callable[[Image.Image, Image.Image, Story], list[str]]] = {
    "metro": _metro, "daily": _daily, "wizard": _wizard, "wanted": _wanted}


def compose(photo: Image.Image, story: Story | None = None, style: str = "metro") -> tuple[Image.Image, list[str]]:
    """Lay out the page (with the style's default story if none is given); also returns the fields
    (in page order) that were cut with "…" to fit."""
    story = default_story(style) if story is None else story
    page = Image.new("RGB", (W, H), "white")
    cut = STYLES[style](page, photo, Story(**{name: _printable(getattr(story, name)) for name in FIELDS}))
    return page, cut


def to_jpeg(page: Image.Image) -> bytes:
    buf = io.BytesIO()
    page.save(buf, "JPEG", quality=92, dpi=(150, 150))
    return buf.getvalue()

