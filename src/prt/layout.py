"""Text layout shared by the printed pages: word wrap, fitting text to a box, columns and glyph checks.

Everything is measured with the built-in page font (``prt.testpage._font``) at 150 dpi on A4.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from itertools import chain, islice
from typing import Callable, Iterator

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
    return list(_wrapped(text, font, width))


def _wrapped(text: str, font, width: int) -> Iterator[str]:
    """The lines of ``_wrap`` one at a time, so a caller can stop once its page is full."""
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if font.getlength(candidate) <= width:
            line = candidate
            continue
        if line:
            yield line
        while len(word) > 1:
            cut = _longest(word, lambda part: font.getlength(part) <= width)
            if cut == len(word):
                break
            yield word[:cut]
            word = word[cut:]
        line = word
    if line:
        yield line


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


@dataclass(frozen=True)
class Cut:
    """A text that was cut with "…" to fit: ``max_fit`` of its ``given`` characters were printed.

    The printed part fits on its own, so the text cut to its first ``max_fit`` characters fits in the same box.
    """
    max_fit: int
    given: int

    def to_dict(self) -> dict[str, int]:
        return {"max_fit": self.max_fit, "given": self.given}


Cuts = dict[str, Cut]  # the fields cut to fit a page, in page order


def _cut(text: str, lines: list[str]) -> Cut:
    """How much of ``text`` (paragraphs split by newlines) the wrapped ``lines`` print before their "…".

    ``lines`` are what ``_wrap`` or ``_paragraphs`` made of ``text``, in order, the last one shortened by
    ``_ellipsize``; a "" line (the gap between two paragraphs) may be left out. Whitespace runs count as one
    character, as they print.
    """
    paras = [" ".join(p.split()) for p in text.split("\n") if p.strip()]
    given = len("\n".join(paras))
    shown = [line for line in lines if line]
    if shown:
        shown[-1] = shown[-1].removesuffix(ELLIPSIS).rstrip()
    i = pos = done = 0  # paragraph, where its printed part ends, and the characters of the paragraphs before it
    for line in filter(None, shown):
        while i < len(paras):
            at = paras[i].find(line, pos)
            if at >= 0 and not paras[i][pos:at].strip():  # the very next text of this paragraph
                pos = at + len(line)
                break
            done, i, pos = done + len(paras[i]) + 1, i + 1, 0
    return Cut(min(done + pos, given), given)


def _fit(text: str, size: int, min_size: int, width: int, max_lines: int):
    """Largest font (size → min_size) that wraps ``text`` into ``max_lines``; cut with "…" at the minimum.

    Returns (font, size, lines, cut), where ``cut`` is None when the text fits, else the ``Cut`` saying how
    much of it was printed.
    """
    for s in range(size, min_size - 1, -2):
        font = _font(s)
        lines = _wrap(text, font, width)
        if len(lines) <= max_lines:
            return font, s, lines, None
    lines = lines[:max_lines - 1] + [_ellipsize(lines[max_lines - 1], font, width)]
    return font, s, lines, _cut(text, lines)


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
             size: int, min_size: int, fill, rule=None, centre: bool = False) -> Cut | None:
    """Flow paragraphs down ``columns`` columns, shrinking the type until it fits, else cutting with "…".

    Returns None when the text fits, else the ``Cut`` saying how much of it was printed.
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
    cut = None
    if len(lines) > capacity:
        lines = lines[:capacity]
        while len(lines) > 1 and not lines[-1]:  # end on text, not on a paragraph gap
            lines.pop()
        lines[-1] = _ellipsize(lines[-1], font, col_w)
        cut = _cut(text, lines)
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


# --- text that runs on over several pages -----------------------------------------------------
# A page holds columns of lines; "" is the blank row between two paragraphs. Nothing is cut with "…":
# whatever does not fit is handed back, word for word, to start the next page.

Columns = list[list[str]]


def _consumed(para: str, lines: list[str]) -> int:
    """Where ``para`` resumes after ``lines``, the first lines ``_wrap`` made of it (each one is a slice of it)."""
    pos = 0
    for line in lines:
        pos = para.index(line, pos) + len(line)
    return pos


def flow_columns(paragraphs: list[str], font, col_w: int, rows: int, columns: int,
                 narrow: tuple[int, int] = (0, 0)) -> tuple[Columns, list[str]]:
    """Fill up to ``columns`` columns of ``rows`` lines with paragraphs, a blank row between paragraphs.

    ``narrow`` is (lines, width): the first lines of the first paragraph wrap to that width, leaving room
    for a drop cap. Text that fits on the page is balanced over the columns like ``_columns``.
    Returns the columns and the leftover paragraphs, the first of which resumes mid-paragraph when a
    paragraph was split.
    """
    paras = [p for p in (" ".join(raw.split()) for raw in paragraphs) if p]
    if rows <= 0 or not paras:
        return [], paras
    capacity = rows * columns
    lines: list[tuple[int, str]] = []  # (paragraph, line); a "" line is the gap before that paragraph
    # Only one line more than the page holds is measured, so a long text costs one page of wrapping per page.
    measured, partial = 0, False
    for i, para in enumerate(paras):
        if len(lines) > capacity:
            break
        measured = i + 1
        if lines:
            lines.append((i, ""))
        n, width = narrow if i == 0 else (0, 0)
        head = _wrap(para, font, width)[:n] if n else []
        rest = para[_consumed(para, head):].lstrip()
        wrapped = chain(head, _wrapped(rest, font, col_w))
        lines += [(i, line) for line in islice(wrapped, capacity + 1 - len(lines))]
        if next(wrapped, None) is not None:
            partial = True
            break
    fits = not partial and measured == len(paras) and len(lines) <= capacity
    per_col = min(rows, _rows(len(lines), columns)) if fits else rows
    cols: Columns = [[]]
    taken = 0
    for _, line in lines:
        if len(cols[-1]) == per_col:
            if len(cols) == columns:
                break
            cols.append([])
        taken += 1
        if line or cols[-1]:  # a paragraph gap at the top of a column is left out
            cols[-1].append(line)
    if taken == len(lines) and not partial:  # gaps left out at column tops can make room for everything
        return cols, paras[measured:]
    i = lines[taken][0] if taken < len(lines) else measured - 1
    done = [text for p, text in lines[:taken] if p == i and text]
    if not done:
        return cols, paras[i:]
    return cols, [paras[i][_consumed(paras[i], done):].lstrip()] + paras[i + 1:]


def paginate(paragraphs: list[str], font, col_w: int, columns: int, first_rows: int, rows: int,
             narrow: tuple[int, int] = (0, 0)) -> Iterator[Columns]:
    """Lay the text out page by page: ``first_rows`` lines per column on page one, ``rows`` after.

    Every word lands on exactly one page; ``narrow`` applies to the first page only (see ``flow_columns``).
    Pages are made as they are asked for, so a caller with a page limit can stop early.
    """
    if rows <= 0:
        raise ValueError("a page needs room for at least one line")
    cols, left = flow_columns(paragraphs, font, col_w, first_rows, columns, narrow)
    yield cols
    while left:
        cols, left = flow_columns(left, font, col_w, rows, columns)
        yield cols


def draw_columns(d: ImageDraw.ImageDraw, cols: Columns, x0: int, y0: int, col_w: int, gutter: int, font,
                 size: int, fill, rule=None, indent: tuple[int, int] = (0, 0)) -> None:
    """Draw columns from ``flow_columns``; ``indent`` (lines, px) shifts the first lines of the first column.

    ``rule`` draws a thin line of that colour between the columns in use.
    """
    for c, col in enumerate(cols):
        x = x0 + c * (col_w + gutter)
        for r, line in enumerate(col):
            dx = indent[1] if c == 0 and r < indent[0] else 0
            d.text((x + dx, y0 + r * _lead(size)), line, font=font, fill=fill)
    if rule:
        depth = y0 + max((len(col) for col in cols), default=0) * _lead(size)
        for c in range(1, len(cols)):
            rx = x0 + c * (col_w + gutter) - gutter // 2
            d.line([(rx, y0), (rx, depth)], fill=rule, width=1)
