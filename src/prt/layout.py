"""Text layout shared by the printed pages: word wrap, fitting text to a box, columns and glyph checks.

Everything is measured with the built-in page font (``prt.testpage._font``) at 150 dpi on A4.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from typing import Callable

from PIL import ImageDraw

from prt.testpage import H, MARGIN, W, _font

CONTENT_W = W - 2 * MARGIN
BOTTOM = H - MARGIN


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


ELLIPSIS = "…"


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
