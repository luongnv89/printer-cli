"""Text-first gazette pages: a vintage masthead over sections of ruled columns.

The photo gazette in :mod:`prt.vintage` is built around photographs. This module keeps its paper, masthead,
type and fitting rules but lays out structured text instead: small-caps section headings, then paragraphs or
items in two columns. Both the morning edition and the commit receipt are one A4 page, so text that still
overflows after the body type shrinks is cut with "…" and reported against the input field it came from.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw

from prt import fonts, vintage
from prt.booth import Story
from prt.layout import Cut, Cuts, ELLIPSIS, _col_width, _cut, _ellipsize, _in_source, _lead, _rows, _wrap, draw_columns


@dataclass(frozen=True)
class TextBlock:
    """One paragraph or item in a section.

    ``key`` is the input field the fit report names when the text is cut. ``label`` is generated metadata
    printed before it (a bullet, or a commit's time and hash) and is not counted in the field's characters.
    """

    text: str
    key: str
    label: str = ""


@dataclass(frozen=True)
class Section:
    """A titled section of text blocks; ``key`` names the section in the fit report."""

    title: str
    blocks: tuple[TextBlock, ...]
    key: str


@dataclass(frozen=True)
class TextGazette:
    """Everything a one-page text gazette prints, already normalised by its producer."""

    masthead: str
    headline: str
    sections: tuple[Section, ...]
    kicker: str = ""
    byline: str = ""
    dateline: str = ""
    edition: str = ""
    tagline: str = ""
    price: str = vintage.PRICE


SECTION_HEAD = 54  # small-caps section title, its rule and the gap before the columns
SECTION_GAP = 18
MIN_CONTENT = 3 * _lead(vintage.BODY_MIN)
MAX_LINES_PER_BLOCK = 24  # a defensive bound for pathological generated metadata; normal input limits are lower

Entry = tuple[int | None, str]  # (block index, wrapped line); None is the blank row before another block
Wrapped = tuple[str, str, list[str]]  # display text, printable display text, its wrapped lines


def _wrapped_entries(blocks: tuple[TextBlock, ...], font, col_w: int) -> tuple[list[Entry], list[Wrapped]]:
    """Wrap every block, keeping which input block each line came from."""
    entries: list[Entry] = []
    wrapped: list[Wrapped] = []
    for index, block in enumerate(blocks):
        display = block.label + block.text
        printable = vintage._printable_face(display, font)
        lines = _wrap(printable, font, col_w)[:MAX_LINES_PER_BLOCK]
        if entries and lines:
            entries.append((None, ""))
        entries += [(index, line) for line in lines]
        wrapped.append((display, printable, lines))
    return entries, wrapped


def _arrange(
    entries: list[Entry], rows: int, columns: int, font, col_w: int
) -> tuple[list[list[Entry]], list[Entry], bool]:
    """Balance wrapped lines over columns of ``rows`` lines; ellipsize the last visible line on overflow."""
    rows = max(1, rows)
    capacity = rows * columns
    last_content = max((i for i, (block, line) in enumerate(entries) if block is not None and line), default=-1)
    overflow = capacity <= last_content
    per_col = max(1, min(rows, _rows(len(entries), columns)) if not overflow else rows)
    cols: list[list[Entry]] = [[]]
    printed: list[Entry] = []
    for entry in entries:
        if len(printed) == capacity:
            break
        if len(cols[-1]) == per_col:
            if len(cols) == columns:
                break
            cols.append([])
        cols[-1].append(entry)
        printed.append(entry)

    if overflow:
        # End on text rather than a paragraph gap, then mark the continuation with an ellipsis.
        while printed and (printed[-1][0] is None or not printed[-1][1]):
            printed.pop()
            for col in reversed(cols):
                if col:
                    col.pop()
                    break
        if printed:
            block, line = printed[-1]
            shortened = (block, _ellipsize(line, font, col_w))
            printed[-1] = shortened
            for col in reversed(cols):
                if col:
                    col[-1] = shortened
                    break
    return cols, printed, overflow


def _cuts(blocks: tuple[TextBlock, ...], wrapped: list[Wrapped], printed: list[Entry], font) -> Cuts:
    """The fields whose text was not printed whole, with how much of each input field was printed."""
    shown: dict[int, list[str]] = {}
    for block, line in printed:
        if block is not None:
            shown.setdefault(block, []).append(line)
    cuts: Cuts = {}
    for index, field in enumerate(blocks):
        display, printable, lines = wrapped[index]
        got = shown.get(index, [])
        complete = len(got) >= len(lines)
        if bool(got) and len(got) <= len(lines) and got[-1] != lines[len(got) - 1]:
            original = lines[len(got) - 1].rstrip()
            last = got[-1]
            # At a block boundary the continuation ellipsis may only have been appended; no character of
            # this block was cut then. When there was no room for it whole words were dropped, which is a cut.
            modified = not (last.endswith(ELLIPSIS) and last[: -len(ELLIPSIS)].rstrip() == original)
        else:
            modified = False
        if complete and not modified:
            continue
        if not got:
            cuts[field.key] = Cut(0, len(field.text))
            continue
        base = _in_source(_cut(printable, got), display, lambda c: vintage._printable_face(c, font))
        cuts[field.key] = Cut(max(0, base.max_fit - len(field.label)), max(0, base.given - len(field.label)))
    return cuts


def _draw_blocks(
    d: ImageDraw.ImageDraw, blocks: tuple[TextBlock, ...], box: tuple[int, int, int, int], columns: int = 2
) -> Cuts:
    """Draw blocks in ruled columns, shrinking Old Standard until they fit; cut and report what remains."""
    if not blocks:
        return {}
    x0, y0, x1, y1 = box
    width, height = x1 - x0, max(1, y1 - y0)
    col_w = _col_width(width, columns, vintage.GUTTER)
    for size in range(vintage.BODY, vintage.BODY_MIN - 1, -1):
        font = fonts.face("body", size)
        entries, wrapped = _wrapped_entries(blocks, font, col_w)
        rows = max(1, height // _lead(size))
        cols, printed, overflow = _arrange(entries, rows, columns, font, col_w)
        if not overflow or size == vintage.BODY_MIN:
            break
    cuts = _cuts(blocks, wrapped, printed, font) if overflow else {}
    draw_columns(
        d,
        [[line for _, line in col] for col in cols],
        x0,
        y0,
        col_w,
        vintage.GUTTER,
        font,
        size,
        vintage.INK,
        rule=vintage.INK,
    )
    return cuts


def _measure_blocks(blocks: tuple[TextBlock, ...], width: int, columns: int = 2) -> int:
    """The height the blocks want at the design body size, before the page apportions room."""
    font = fonts.face("body", vintage.BODY)
    entries, _ = _wrapped_entries(blocks, font, _col_width(width, columns, vintage.GUTTER))
    return max(1, _rows(len(entries), columns)) * _lead(vintage.BODY)


def _content_heights(sections: tuple[Section, ...], width: int, available: int) -> list[int]:
    """Apportion the page's remaining height between sections, compressing them proportionally when needed."""
    wanted = [max(MIN_CONTENT, _measure_blocks(section.blocks, width)) for section in sections]
    budget = max(len(sections), available - len(sections) * (SECTION_HEAD + SECTION_GAP))
    if sum(wanted) <= budget:
        return wanted
    scale = budget / sum(wanted)
    heights = [max(MIN_CONTENT // 2, int(height * scale)) for height in wanted]
    # Rounding and the floor can still exceed a very small budget; shrink linearly in that rare case.
    if sum(heights) > budget:
        heights = [max(_lead(vintage.BODY_MIN), int(height * budget / sum(heights))) for height in heights]
    return heights


def _draw_section(d: ImageDraw.ImageDraw, section: Section, box: tuple[int, int, int, int], sheet) -> Cuts:
    """One section heading and its blocks inside ``box``; returns every cut in it."""
    x0, y0, x1, y1 = box
    width = x1 - x0
    cuts: Cuts = {}
    runs, size, title_cut = vintage._fit_caps(section.title, "body-bold", 24, 16, width)
    vintage._draw_caps(d, (sheet.width - vintage._caps_width(runs)) / 2, y0 + size, runs)
    if title_cut:
        cuts[f"{section.key}.title"] = title_cut
    vintage._rule(d, y0 + size + 10, sheet)
    cuts.update(_draw_blocks(d, section.blocks, (x0, y0 + SECTION_HEAD, x1, y1)))
    return cuts


def blank_page(sheet=vintage.PORTRAIT) -> Image.Image:
    """A white A4 sheet with the cream printable area the gazette uses."""
    page = Image.new("RGB", (sheet.width, sheet.height), "white")
    ImageDraw.Draw(page).rectangle(
        [sheet.inset, sheet.inset, sheet.width - sheet.inset - 1, sheet.height - sheet.inset - 1], fill=vintage.PAPER
    )
    return page


def compose_page(document: TextGazette) -> tuple[Image.Image, Cuts]:
    """Lay ``document`` out as one upright A4 page, returning ``(page, cut)``.

    The producer decides the sections and the input-field key of every block. Header fields use the photo
    gazette's names (``kicker``, ``headline``, ``byline``); section titles are ``<section>.title`` and block
    keys are used unchanged, so a morning item can be named ``calendar.items[0]`` and a commit
    ``commits[3].subject``.
    """
    if not document.sections:
        raise ValueError("a text gazette needs at least one section")
    sheet = vintage.PORTRAIT
    page = blank_page(sheet)
    d = ImageDraw.Draw(page)
    head_story = Story(headline=document.headline, story="", kicker=document.kicker, byline=document.byline)
    y, cuts = vintage.draw_head(
        d,
        head_story,
        sheet,
        masthead=document.masthead,
        tagline=document.tagline or vintage.TAGLINE,
        dateline=document.dateline or None,
        edition=document.edition or vintage.EDITION,
        price=document.price,
    )
    left, right = sheet.margin, sheet.width - sheet.margin
    bottom = sheet.bottom - 52
    heights = _content_heights(document.sections, sheet.content_w, bottom - y)
    for section, content_h in zip(document.sections, heights):
        end = min(bottom, y + SECTION_HEAD + content_h)
        cuts.update(_draw_section(d, section, (left, y, right, end), sheet))
        y = end + SECTION_GAP
    vintage._page_mark(d, 1, 1, sheet)
    return page, cuts
