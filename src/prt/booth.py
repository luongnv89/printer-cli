"""Photo booth: a photo of a person and a short news story, laid out as a newspaper front page.

Rendered as an A4 JPEG at 150 dpi, like the test page, so it prints straight over IPP.
Two styles: ``metro`` (colour tabloid, two columns) and ``daily`` (black-and-white broadsheet,
three columns). Without a story the page uses DEFAULT_STORY.
"""

from __future__ import annotations

import io
import json
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


def _clean(text: str, keep_newlines: bool) -> str:
    """Drop control characters; keep newlines (paragraph breaks, blank lines dropped) only where asked."""
    text = text.replace("\r\n", "\n").replace("\t", " ")
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


def _columns_height(text: str, width: int, columns: int, gutter: int, size: int) -> int:
    """Height the text needs in balanced columns at type ``size``."""
    lines = _paragraphs(text, _font(size), _col_width(width, columns, gutter))
    return -(-len(lines) // columns) * _lead(size)


def _columns(d: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], columns: int, gutter: int,
             size: int, min_size: int, fill, rule=None) -> bool:
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
    rows = -(-len(lines) // columns)  # balance the columns, like a real page
    col = row = 0
    for line in lines:
        if row == rows:
            col, row = col + 1, 0
        if not line and row == 0:
            continue
        d.text((x0 + col * (col_w + gutter), y0 + row * _lead(s)), line, font=font, fill=fill)
        row += 1
    if rule:
        for c in range(1, columns):
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
    # The frame is drawn 4 px outside the photo, so a full-width photo stays 4 px inside each edge.
    px0, py0, px1, py1 = _photo_box(photo, y, 560, CONTENT_W - 8)
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
        font, size, lines, cut_byline = _fit(story.byline.upper(), 26, 20, CONTENT_W, 1)
        y = _text_block(d, left, y, lines, font, size, RED, bold=1) + 6
        cut += ["byline"] * cut_byline
    d.rectangle([left, BOTTOM - 8, right, BOTTOM], fill=RED)
    if _columns(d, story.story, (left, y, right, BOTTOM - 24), 2, 44, 28, 16, INK):
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


STYLES: dict[str, Callable[[Image.Image, Image.Image, Story], list[str]]] = {"metro": _metro, "daily": _daily}


def compose(photo: Image.Image, story: Story = DEFAULT_STORY, style: str = "metro") -> tuple[Image.Image, list[str]]:
    """Lay out the page; also returns the fields (in page order) that were cut with "…" to fit."""
    page = Image.new("RGB", (W, H), "white")
    cut = STYLES[style](page, photo, Story(**{name: _printable(getattr(story, name)) for name in FIELDS}))
    return page, cut


def to_jpeg(page: Image.Image) -> bytes:
    buf = io.BytesIO()
    page.save(buf, "JPEG", quality=92, dpi=(150, 150))
    return buf.getvalue()

