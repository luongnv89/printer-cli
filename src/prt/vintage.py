"""Vintage gazette: one photo or many, laid out as the pages of an old newspaper.

The look follows the printed gazette at luongnv.com/news: cream paper and a single dark ink, a blackletter
masthead between hairline and double rules, a small-caps dateline, headlines in Playfair Display capitals
and Old Standard TT text in ruled columns, with no textures or colour. Photos are toned from ink to paper.

The front page holds the masthead, the story and up to ``FIRST`` photos: the lead picture and a row of two
plates. Each later page holds up to ``PER_PAGE`` plates in a ruled grid, so every photo is printed.
Pages are A4 at 150 dpi like the booth pages, upright (``PORTRAIT``, a 2 × 3 grid of plates) or turned on its
side (``LANDSCAPE``, a 3 × 2 grid, the lead beside the story), and each one prints as its own JPEG job.

Every word is drawn in a bundled font (``prt.fonts``), and every character is checked against the font that
draws it: what a font lacks becomes a stand-in, its unaccented form, or "?", never an empty box.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from prt import booth, fonts
from prt.booth import Story
from prt.drivers.base import PrinterError
from prt.layout import (BOTTOM, CONTENT_W, ELLIPSIS, _STAND_INS, _col_width, _ellipsize, _lead, _paragraphs, _rows,
                        _text_block, _wrap, draw_columns, flow_columns)
from prt.testpage import H, MARGIN, W

PAPER = (244, 236, 216)  # #f4ecd8
INK = (26, 26, 26)  # #1a1a1a
RULE = 2  # a hairline: about one CSS pixel at 150 dpi
INSET = 45  # the cream stops this far inside the sheet: the printer cannot print to the edge, so it leaves white


@dataclass(frozen=True)
class Sheet:
    """The page the gazette is laid out on, in pixels at 150 dpi."""
    width: int
    height: int
    margin: int
    content_w: int  # between the margins
    bottom: int  # lowest y text and photos reach
    inset: int  # white rim round the cream
    masthead_max: int  # largest masthead size: a landscape page has less height to spend on it
    headline_lines: int
    plate_cols: int  # plates side by side on a plate page


PORTRAIT = Sheet(W, H, MARGIN, CONTENT_W, BOTTOM, INSET, masthead_max=132, headline_lines=3, plate_cols=2)
LANDSCAPE = Sheet(H, W, MARGIN, H - 2 * MARGIN, W - MARGIN, INSET, masthead_max=96, headline_lines=2, plate_cols=3)

FIRST = 3  # photos on the front page: the lead picture and a row of two plates
PER_PAGE = 6  # plates on every later page, in a 2 × 3 grid
MAX_PAGES = 10
MAX_PHOTOS = FIRST + (MAX_PAGES - 1) * PER_PAGE  # more is refused, never cut short
LEAD_LIMIT = PORTRAIT.content_w  # longest side a photo is kept at once decoded: the lead is never wider than the text
PLATE_LIMIT = 540  # and a plate never wider than half of it

MASTHEAD = "The Photo Gazette"
TAGLINE = "Pictures, people, and matters of consequence"
EDITION = "Vol. I · No. 1"
PRICE = "Price: Complimentary"

DEFAULT_GAZETTE_STORY = Story(
    kicker="Special edition",
    headline="Pictures From a Day Worth Remembering",
    story=("Every picture in this edition was brought to our offices this morning and set in type at once. "
           "The first leads the page as Fig. 1, and any others follow as numbered plates for closer study.\n"
           "Our correspondents report fair weather, good company and not a single blurred frame. The editors "
           "have printed every picture in full, in keeping with the custom of this paper.\n"
           "Further editions will follow as more pictures arrive."),
    caption="Our lead photograph, as it came off the press.",
    byline="By the prt gazette desk",
)

GUTTER = 40
BODY, BODY_MIN = 24, 16  # story type: design size, and the smallest it shrinks to before text is cut
LEAD_MIN = 300  # room kept for the lead's height, grown into what a short story leaves (a wide lead may be shallower)
LEAD_MAX, LEAD_MAX_SOLO = 520, 760  # largest lead height with a row of plates under it, and alone
PLATE_H = 270  # height of the plate row on the front page
FRAME = 6  # the hairline frame sits this far outside a photo
# On a landscape front page the lead's column takes at most this share of the width (so the lead stays well
# under LEAD_LIMIT) and narrows to a narrow lead, but never below LEAD_COL_MIN, which keeps room for its caption.
LEAD_SHARE, LEAD_COL_MIN = 0.58, 480
PLATE_H_MIN, PLATE_H_MAX = 160, 260  # plate row under a landscape story: grown into what a short story leaves
STORY_COL_MIN = 300  # narrowest story column on a landscape front page: wider story room gets a third column


# --- which photo goes where --------------------------------------------------------------------


def plan_pages(n: int) -> list[list[int]]:
    """The photo numbers on each page: the first ``FIRST`` on page 1, then ``PER_PAGE`` a page, in order."""
    if n < 1:
        raise ValueError("a gazette needs at least one photo")
    return [list(range(min(n, FIRST)))] + [list(range(i, min(n, i + PER_PAGE))) for i in range(FIRST, n, PER_PAGE)]


def check_count(n: int) -> None:
    """Refuse (exit code 3) more photos than ``MAX_PAGES`` pages hold, rather than leave any out."""
    if n > MAX_PHOTOS:
        raise PrinterError(f"gazette takes at most {MAX_PHOTOS} photos, got {n}",
                           hint=f"print them in batches of up to {MAX_PHOTOS} ({MAX_PAGES} pages each)", exit_code=3)


def page_paths(path: Path, fmt: str, count: int) -> list[Path]:
    """The files ``article.page_files`` writes for ``count`` pages, named before any page exists."""
    if fmt == "pdf" or count == 1:
        return [path]
    return [path.with_name(f"{path.stem}-{n}{path.suffix}") for n in range(1, count + 1)]


def check_outputs(outputs: list[Path], photos: list[Path]) -> None:
    """Refuse (exit code 2) an output file that is one of the photos, so saving a page never destroys one."""
    for target in outputs:
        for photo in photos:
            if _same_file(target, photo):
                raise PrinterError(f"output {target} would overwrite input photo {photo}",
                                   hint="save the pages under another name", exit_code=2)


def _same_file(target: Path, photo: Path) -> bool:
    if target.exists():  # also catches hard links, and other spellings on a case-insensitive disk
        return photo.exists() and os.path.samefile(target, photo)
    a, b = (unicodedata.normalize("NFC", str(path.resolve())) for path in (target, photo))
    return a == b or (a.casefold() == b.casefold() and _ignores_case(target.resolve().parent))


def _ignores_case(folder: Path) -> bool:
    flipped = Path(str(folder).swapcase())
    return flipped != folder and flipped.exists() and os.path.samefile(folder, flipped)


# --- photos ------------------------------------------------------------------------------------


def _downscale(photo: Image.Image, limit: int) -> Image.Image:
    """A copy whose longest side is at most ``limit`` px (never enlarged); ``photo`` itself is left as it is."""
    small = photo.copy()
    small.thumbnail((limit, limit))
    return small


def load_photos(paths: list[Path]) -> list[Image.Image]:
    """Decode every photo now (exit code 3 on the first bad one), each shrunk to the most it prints at.

    One at a time, and nothing keeps the full-size decode once its small copy exists, so a stack of large
    photos never sits in memory at full size.
    """
    return [_downscale(booth.load_photo(path), LEAD_LIMIT if i == 0 else PLATE_LIMIT) for i, path in enumerate(paths)]


def _treat(photo: Image.Image) -> Image.Image:
    """Tone the photo from ink (shadows) to paper (highlights), like a halftone on newsprint."""
    return ImageOps.colorize(ImageOps.grayscale(photo), black=INK, white=PAPER)


def _place(page: Image.Image, photo: Image.Image, box: tuple[int, int, int, int]) -> None:
    """Fill the box (cropping, a little more off the bottom than the top, only if its shape differs), tone, frame."""
    x0, y0, x1, y1 = box
    fitted = ImageOps.fit(photo, (x1 - x0, y1 - y0), method=Image.Resampling.LANCZOS, centering=(0.5, 0.1))
    page.paste(_treat(fitted), (x0, y0))
    ImageDraw.Draw(page).rectangle([x0 - FRAME, y0 - FRAME, x1 + FRAME - 1, y1 + FRAME - 1], outline=INK,
                                   width=RULE)


def _contain(photo: Image.Image, top: int, x0: int, width: int, height: int) -> tuple[int, int, int, int]:
    """The largest box of the photo's shape inside width × height at (x0, top), centred."""
    scale = min(width / photo.width, height / photo.height)
    w, h = max(1, round(photo.width * scale)), max(1, round(photo.height * scale))
    bx, by = x0 + (width - w) // 2, top + (height - h) // 2
    return bx, by, bx + w, by + h


# --- type --------------------------------------------------------------------------------------


@lru_cache(maxsize=64)
def _notdef(face: ImageFont.FreeTypeFont) -> bytes:
    return bytes(face.getmask("\U0010fffd"))


def _has(face: ImageFont.FreeTypeFont, c: str) -> bool:
    return bytes(face.getmask(c)) != _notdef(face)


@lru_cache(maxsize=16384)
def _char_in(face: ImageFont.FreeTypeFont, c: str) -> str:
    """What ``face`` draws for ``c``: itself, a stand-in, its unaccented form, or "?"."""
    if unicodedata.combining(c):
        return ""  # a separate accent mark is dropped, like a composed one
    if c.isspace():
        return " "
    if _has(face, c):
        return c
    base = "".join(b for b in unicodedata.normalize("NFKD", c) if not unicodedata.combining(b))
    for other in (_STAND_INS.get(c, ""), base):
        if other and all(_has(face, b) for b in other):
            return other
    return "?"


def _printable_face(text: str, face: ImageFont.FreeTypeFont) -> str:
    """``text`` with every character ``face`` cannot draw replaced (see ``_char_in``)."""
    return "".join(_char_in(face, c) for c in text)


def _fit_face(text: str, role: str, size: int, min_size: int, width: int, max_lines: int):
    """Largest size (size → min_size) of ``role`` that wraps ``text`` into ``max_lines``; cut with "…" at the
    smallest. Returns (font, size, lines, cut)."""
    for s in range(size, min_size - 1, -2):
        font = fonts.face(role, s)
        lines = _wrap(_printable_face(text, font), font, width)
        if len(lines) <= max_lines:
            return font, s, lines, False
    return font, s, lines[:max_lines - 1] + [_ellipsize(lines[max_lines - 1], font, width)], True


def _centred(d: ImageDraw.ImageDraw, y: int, text: str, role: str, size: int, min_size: int, lines: int,
             x0: int, width: int) -> tuple[int, bool]:
    """Fit ``text`` in ``lines`` lines centred in ``width`` from ``x0``; returns (y below, cut)."""
    font, s, wrapped, cut = _fit_face(text, role, size, min_size, width, lines)
    return _text_block(d, x0, y, wrapped, font, s, INK, width=width), cut


Runs = list[tuple[ImageFont.FreeTypeFont, str]]


def _caps(text: str, role: str, size: int) -> Runs:
    """Small capitals as (font, text) runs: capitals at ``size``, lower-case letters as capitals at 3/4 size."""
    runs: Runs = []
    for c in text:
        font = fonts.face(role, round(size * 0.75) if c.islower() else size)
        glyphs = _printable_face(c.upper(), font)  # upper first: it can give letters the font lacks
        if runs and runs[-1][0] is font:
            runs[-1] = (font, runs[-1][1] + glyphs)
        else:
            runs.append((font, glyphs))
    return runs


def _caps_width(runs: Runs) -> float:
    return sum(font.getlength(text) for font, text in runs)


def _draw_caps(d: ImageDraw.ImageDraw, x: float, baseline: float, runs: Runs) -> None:
    for font, text in runs:
        if text:
            d.text((x, baseline), text, font=font, fill=INK, anchor="ls")  # one shared baseline
        x += font.getlength(text)


def _fit_caps(text: str, role: str, size: int, min_size: int, width: int) -> tuple[Runs, int, bool]:
    """Small caps shrunk (size → min_size) to fit ``width``, then cut with "…". Returns (runs, size, cut)."""
    for s in range(size, min_size - 1, -1):
        runs = _caps(text, role, s)
        if _caps_width(runs) <= width:
            return runs, s, False
    while text and _caps_width(_caps(text + ELLIPSIS, role, s)) > width:
        text = text[:-1]
    return _caps(text.rstrip() + ELLIPSIS, role, s), s, True


def _rule(d: ImageDraw.ImageDraw, y: int, sheet: Sheet) -> None:
    """A hairline from margin to margin."""
    d.line([(sheet.margin, y), (sheet.width - sheet.margin, y)], fill=INK, width=RULE)


def _double(d: ImageDraw.ImageDraw, y: int, sheet: Sheet) -> int:
    """A double rule from y; returns the y below it."""
    _rule(d, y, sheet)
    _rule(d, y + 5, sheet)
    return y + 7


# --- pages -------------------------------------------------------------------------------------

Placement = tuple[int, tuple[int, int, int, int]]  # (photo number, box it was printed in)


def _story(d: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], columns: int = 2) -> bool:
    """The story in ruled columns, shrinking the type until it fits, else cut with "…"; True when cut."""
    x0, y0, x1, y1 = box
    col_w = _col_width(x1 - x0, columns, GUTTER)
    for s in range(BODY, BODY_MIN - 1, -1):
        font = fonts.face("body", s)
        cols, left = flow_columns([_printable_face(p, font) for p in text.split("\n")], font, col_w,
                                  (y1 - y0) // _lead(s), columns)
        if not left:
            break
    if left:
        last = cols[-1]
        while len(last) > 1 and not last[-1]:  # end on text, not on a paragraph gap
            last.pop()
        last[-1] = _ellipsize(last[-1], font, col_w)
    draw_columns(d, cols, x0, y0, col_w, GUTTER, font, s, INK)
    # The column rule is drawn here rather than by draw_columns, at the weight of every other rule.
    depth = y0 + max((len(col) for col in cols), default=0) * _lead(s)
    for c in range(1, len(cols)):
        rx = x0 + c * (col_w + GUTTER) - GUTTER // 2
        d.line([(rx, y0), (rx, depth)], fill=INK, width=RULE)
    return bool(left)


def _story_height(text: str, size: int, width: int, columns: int = 2) -> int:
    font = fonts.face("body", size)
    lines = _paragraphs("\n".join(_printable_face(p, font) for p in text.split("\n")), font,
                        _col_width(width, columns, GUTTER))
    return _rows(len(lines), columns) * _lead(size)


def _page_mark(d: ImageDraw.ImageDraw, number: int, total: int, sheet: Sheet) -> None:
    """Closing double rule and "Page n of N" at the foot of the page."""
    y = _double(d, sheet.bottom - 36, sheet)
    text = f"Page {number} of {total}" + (f" · continued on page {number + 1}" if number < total else "")
    runs = _caps(text, "body-bold", 18)
    _draw_caps(d, (sheet.width - _caps_width(runs)) / 2, y + 24, runs)


def _front(page: Image.Image, photos: list[Image.Image], story: Story, indices: list[int], total: int,
           sheet: Sheet) -> tuple[list[Placement], list[str]]:
    """Masthead, dateline and headline across the page, then the lead photo, a row of up to two plates and the
    story in ruled columns: stacked on an upright page, side by side on a landscape one."""
    d = ImageDraw.Draw(page)
    y, cut = _head(d, story, sheet)
    body = _wide_front if sheet.width > sheet.height else _tall_front
    placements, cut_below = body(page, d, photos, story, indices, y, sheet)
    _page_mark(d, 1, total, sheet)
    return placements, cut + cut_below


def _head(d: ImageDraw.ImageDraw, story: Story, sheet: Sheet) -> tuple[int, list[str]]:
    """Masthead, tagline, dateline, kicker, headline and byline across the page; returns (y below, cut)."""
    left, width, right = sheet.margin, sheet.content_w, sheet.width - sheet.margin
    cut: list[str] = []
    y = sheet.margin
    _rule(d, y, sheet)
    y += 18
    size = next(s for s in range(sheet.masthead_max, 40, -4)
                if fonts.face("masthead", s).getlength(MASTHEAD) <= width - 80)
    mast = fonts.face("masthead", size)
    top, bottom = mast.getbbox(MASTHEAD)[1::2]
    d.text(((sheet.width - mast.getlength(MASTHEAD)) / 2, y - top), MASTHEAD, font=mast, fill=INK)
    y += bottom - top + 16
    y, _ = _centred(d, y, TAGLINE, "body-italic", 24, 24, 1, left, width)
    y = _double(d, y + 4, sheet)
    parts = [_caps(text, "body-bold", 18) for text in (booth._today(), EDITION, PRICE)]
    baseline = y + 30
    _draw_caps(d, left, baseline, parts[0])
    _draw_caps(d, (sheet.width - _caps_width(parts[1])) / 2, baseline, parts[1])
    _draw_caps(d, right - _caps_width(parts[2]), baseline, parts[2])
    y = _double(d, baseline + 12, sheet) + 22

    if story.kicker:
        runs, ks, cut_kicker = _fit_caps(story.kicker, "body-bold", 24, 16, width)
        _draw_caps(d, (sheet.width - _caps_width(runs)) / 2, y + ks, runs)
        y += ks + 18
        cut += ["kicker"] * cut_kicker
    y, cut_headline = _centred(d, y, story.headline.upper(), "display-black", 64, 36, sheet.headline_lines, left,
                                width)
    cut += ["headline"] * cut_headline
    _rule(d, y + 4, sheet)
    y += 18
    if story.byline:
        y, cut_byline = _centred(d, y, story.byline, "body-italic", 22, 18, 1, left, width)
        cut += ["byline"] * cut_byline
    return y + 8, cut


def _tall_front(page: Image.Image, d: ImageDraw.ImageDraw, photos: list[Image.Image], story: Story,
                indices: list[int], y: int, sheet: Sheet) -> tuple[list[Placement], list[str]]:
    """Upright: the lead photo, a row of up to two plates under it, then the story in two ruled columns."""
    left, width, right = sheet.margin, sheet.content_w, sheet.width - sheet.margin
    cut: list[str] = []
    lead, plates = photos[indices[0]], indices[1:]
    cap = _fit_face(f"Fig. {indices[0] + 1}. {story.caption}" if story.caption else f"Fig. {indices[0] + 1}",
                    "body-italic", 22, 18, width - 80, 2)
    cut += ["caption"] * cap[3]
    caption_h = len(cap[2]) * _lead(cap[1])
    plates_h = 10 + PLATE_H + 2 * FRAME + 6 + _lead(20) if plates else 0
    story_bottom = sheet.bottom - 52
    # Room for the lead and the story: below the lead come its frame, caption, the plate row and a rule.
    room = story_bottom - y - (2 * FRAME + 6 + caption_h + plates_h + 36)
    # A short story leaves paper to spare: the lead grows into it, up to the full width of a wide photo.
    largest = min(LEAD_MAX if plates else LEAD_MAX_SOLO, round((width - 2 * FRAME) * lead.height / lead.width))
    height = max(LEAD_MIN, min(largest, room - _story_height(story.story, BODY, width)))
    # The whole photo, never cropped: a portrait lead is narrower, a panorama shallower.
    x0, top, x1, bottom = _contain(lead, y + FRAME, left + FRAME, width - 2 * FRAME, height)
    box = (x0, y + FRAME, x1, y + FRAME + bottom - top)  # set at the top: what it leaves goes to the story
    _place(page, lead, box)
    placements = [(indices[0], box)]
    y = _text_block(d, left + 40, box[3] + FRAME + 6, cap[2], cap[0], cap[1], INK, width=width - 80)

    if plates:
        y += 10
        cell_w = _col_width(width, 2, GUTTER)
        cells = [left, left + cell_w + GUTTER] if len(plates) == 2 else [(sheet.width - cell_w) // 2]
        label_y = y + PLATE_H + 2 * FRAME + 6
        for k, cx in zip(plates, cells):
            box = _contain(photos[k], y + FRAME, cx + FRAME, cell_w - 2 * FRAME, PLATE_H)
            _place(page, photos[k], box)
            placements.append((k, box))
            _centred(d, label_y, f"Fig. {k + 1}", "body-italic", 20, 20, 1, cx, cell_w)
        if len(plates) == 2:
            rx = left + cell_w + GUTTER // 2
            d.line([(rx, y), (rx, label_y + _lead(20))], fill=INK, width=RULE)
        y = label_y + _lead(20)

    y += 16
    _rule(d, y, sheet)
    if _story(d, story.story, (left, y + 20, right, story_bottom)):
        cut.append("story")
    return placements, cut


def _wide_front(page: Image.Image, d: ImageDraw.ImageDraw, photos: list[Image.Image], story: Story,
                indices: list[int], y: int, sheet: Sheet) -> tuple[list[Placement], list[str]]:
    """Landscape: the lead photo down the left with its caption, the story in ruled columns on the right, and
    the row of up to two plates under the story."""
    left, right = sheet.margin, sheet.width - sheet.margin
    story_bottom = sheet.bottom - 52
    y += 10
    cut: list[str] = []
    lead, plates = photos[indices[0]], indices[1:]
    # Two caption lines are kept whatever the lead's width turns out to be, so the lead is sized in one pass.
    caption_room = 2 * _lead(22)
    x0, top, x1, bottom = _contain(lead, 0, 0, round(sheet.content_w * LEAD_SHARE) - 2 * FRAME,
                                   story_bottom - y - 2 * FRAME - 6 - caption_room)
    # The whole photo, never cropped; a narrow lead narrows its column and leaves the story more room.
    col_w = max(x1 - x0 + 2 * FRAME, LEAD_COL_MIN)
    bx = left + (col_w - (x1 - x0)) // 2
    box = (bx, y + FRAME, bx + x1 - x0, y + FRAME + bottom - top)
    _place(page, lead, box)
    placements = [(indices[0], box)]
    cap = _fit_face(f"Fig. {indices[0] + 1}. {story.caption}" if story.caption else f"Fig. {indices[0] + 1}",
                    "body-italic", 22, 18, col_w - 40, 2)
    cut += ["caption"] * cap[3]
    _text_block(d, left + 20, box[3] + FRAME + 6, cap[2], cap[0], cap[1], INK, width=col_w - 40)
    rx = left + col_w + GUTTER // 2
    d.line([(rx, y), (rx, story_bottom)], fill=INK, width=RULE)

    x0 = left + col_w + GUTTER
    width = right - x0
    columns = 3 if _col_width(width, 3, GUTTER) >= STORY_COL_MIN else 2
    story_end = story_bottom
    if plates:
        label = 6 + _lead(20)
        spare = story_bottom - y - _story_height(story.story, BODY, width, columns) - 2 * FRAME - label - 36
        plate_h = max(PLATE_H_MIN, min(PLATE_H_MAX, spare))
        py = story_bottom - label - 2 * FRAME - plate_h  # the plate row sits on the foot of the column
        cell_w = _col_width(width, 2, GUTTER)
        cells = [x0, x0 + cell_w + GUTTER] if len(plates) == 2 else [x0 + (width - cell_w) // 2]
        label_y = py + plate_h + 2 * FRAME + 6
        for k, cx in zip(plates, cells):
            plate = _contain(photos[k], py + FRAME, cx + FRAME, cell_w - 2 * FRAME, plate_h)
            _place(page, photos[k], plate)
            placements.append((k, plate))
            _centred(d, label_y, f"Fig. {k + 1}", "body-italic", 20, 20, 1, cx, cell_w)
        if len(plates) == 2:
            mid = x0 + cell_w + GUTTER // 2
            d.line([(mid, py), (mid, label_y + _lead(20))], fill=INK, width=RULE)
        d.line([(x0, py - 16), (right, py - 16)], fill=INK, width=RULE)
        story_end = py - 36
    if _story(d, story.story, (x0, y, right, story_end), columns):
        cut.append("story")
    return placements, cut


def _plates(page: Image.Image, photos: list[Image.Image], indices: list[int], number: int, total: int,
            sheet: Sheet) -> list[Placement]:
    """Running head with the page mark, a section title, then up to six plates in a ruled grid (2 × 3 upright,
    3 × 2 landscape), each with its figure number."""
    d = ImageDraw.Draw(page)
    left, width = sheet.margin, sheet.content_w
    head = fonts.face("masthead", 48)
    top, bottom = head.getbbox(MASTHEAD)[1::2]
    d.text((left, sheet.margin - top), MASTHEAD, font=head, fill=INK)
    mark = _caps(f"{booth._today()} · Page {number} of {total}", "body-bold", 18)
    _draw_caps(d, sheet.width - left - _caps_width(mark), sheet.margin + bottom - top, mark)
    y = _double(d, sheet.margin + bottom - top + 14, sheet) + 14
    first, last = indices[0] + 1, indices[-1] + 1
    y, _ = _centred(d, y, f"PLATES {first}–{last}" if last > first else f"PLATE {first}", "display-bold", 32, 32, 1,
                    left, width)
    _rule(d, y + 2, sheet)
    y += 22

    cols = sheet.plate_cols
    cell_w = _col_width(width, cols, GUTTER)
    rows = -(-len(indices) // cols)  # only the rows in use share the page
    cell_h = (sheet.bottom - 16 - y) // rows
    label = _lead(20) + 12
    # A photo box no taller than a plate is kept, so a page of one or two never blows a plate up soft.
    box_h = min(cell_h - 32 - 2 * FRAME - label, PLATE_LIMIT)
    placements = []
    for row in range(rows):
        pair = indices[cols * row:cols * row + cols]
        # A short last row is centred, so a lone last plate sits in the middle, as on the front page.
        start = left + (cols - len(pair)) * (cell_w + GUTTER) // 2
        xs = [start + i * (cell_w + GUTTER) for i in range(len(pair))]
        sizes = [_contain(photos[k], 0, 0, cell_w - 2 * FRAME, box_h) for k in pair]
        tallest = max(b[3] - b[1] for b in sizes)
        foot = y + row * cell_h + (cell_h + tallest - label) // 2  # one foot a row, so its labels share a line
        for k, cx, (x0, top, x1, bottom) in zip(pair, xs, sizes):
            box = (cx + FRAME + x0, foot - (bottom - top), cx + FRAME + x1, foot)
            _place(page, photos[k], box)
            placements.append((k, box))
            _centred(d, foot + FRAME + 10, f"Fig. {k + 1}", "body-italic", 20, 20, 1, cx, cell_w)
    for row in range(1, rows):
        _rule(d, y + row * cell_h, sheet)
    full = len(indices) // cols
    if full:  # down the full rows, never through a lone plate
        for c in range(1, cols):
            rx = left + c * (cell_w + GUTTER) - GUTTER // 2
            d.line([(rx, y), (rx, y + full * cell_h)], fill=INK, width=RULE)
    if len(pair) > 1 and full < rows:  # between the plates of a short, centred last row
        for x in xs[1:]:
            d.line([(x - GUTTER // 2, y + full * cell_h), (x - GUTTER // 2, y + rows * cell_h)], fill=INK,
                   width=RULE)
    _double(d, sheet.bottom - 7, sheet)
    return placements


def compose_pages(photos: list[Image.Image], story: Story | None = None, landscape: bool = False
                  ) -> tuple[list[Image.Image], list[list[Placement]], list[str]]:
    """Lay the photos out over the pages ``plan_pages`` gives (with the default story if none is given), on
    upright A4 or, with ``landscape``, A4 on its side.

    Returns (pages, placements, cut): ``placements[p]`` lists (photo number, box) for page p, and ``cut``
    names, in page order, the story fields shortened with "…" to fit the front page.
    """
    sheet = LANDSCAPE if landscape else PORTRAIT
    story = DEFAULT_GAZETTE_STORY if story is None else story
    plan = plan_pages(len(photos))
    pages, placements, cut = [], [], []
    for number, indices in enumerate(plan, start=1):
        page = Image.new("RGB", (sheet.width, sheet.height), "white")
        ImageDraw.Draw(page).rectangle([sheet.inset, sheet.inset, sheet.width - sheet.inset - 1,
                                        sheet.height - sheet.inset - 1], fill=PAPER)
        if number == 1:
            placed, cut = _front(page, photos, story, indices, len(plan), sheet)
        else:
            placed = _plates(page, photos, indices, number, len(plan), sheet)
        pages.append(page)
        placements.append(placed)
    return pages, placements, cut


def for_printer(page: Image.Image) -> Image.Image:
    """The page as the printer takes it, upright A4: a landscape page is turned a quarter turn.

    ROTATE_90 turns it counter-clockwise, as IPP's "landscape" orientation does, so the masthead runs up the
    sheet's left edge and the page reads once the sheet is turned clockwise. An upright page is returned as is.
    """
    return page.transpose(Image.Transpose.ROTATE_90) if page.width > page.height else page
