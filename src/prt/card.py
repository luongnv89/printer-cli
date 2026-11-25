"""Greeting card: a photo and a verse on one A4 sheet that folds into an A6 card.

The sheet is cut into four A6 panels: the front (the photo and the greeting), the inside left (an optional
message, else left blank to write on), the inside right (the verse and the signature) and the back. Folded in half
from top to bottom with the printed side out, then in half again with the photo on the front, the sheet becomes the
card with every panel the right way up. That needs the top row printed upside down: the first fold turns the top
half over about the sheet's horizontal centre line, the second about its vertical one, and the two together turn
the top half a half turn in the plane of the card. ``impose`` places the panels; ``compose`` draws and places them.

Rendered as an A4 JPEG at 150 dpi, like the booth page, so it prints single-sided straight over IPP.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, fields, replace
from pathlib import Path

from PIL import Image, ImageDraw

from prt.booth import STDIN, _grow, _photo, _star, read_stdin
from prt.drivers.base import PrinterError
from prt.layout import (
    Cut,
    Cuts,
    _clean,
    _columns,
    _columns_height,
    _consumed,
    _ellipsize,
    _fit,
    _glyphs,
    _in_source,
    _lead,
    _printable,
    _text_block,
    _wrap,
)
from prt.testpage import H, W, _font

# --- the card text -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    greeting: str
    verse: str
    message: str = ""
    signature: str = ""
    back: str = ""


FIELDS = tuple(f.name for f in fields(Card))
REQUIRED = ("greeting", "verse")
# Upper bounds that keep the text on its panel; text that still overflows its box ends in "…", and compose()
# names the fields that were cut, with how much of each was printed, as prt booth does.
LIMITS = {"greeting": 60, "verse": 600, "message": 300, "signature": 80, "back": 80}
MAX_CARD_FILE = 64 * 1024
# Fields that keep their line breaks: each line of the verse is its own line (a blank line between two stanzas),
# and each line of the message starts a paragraph.
MULTILINE = ("verse", "message")
KIND = "greeting card"
FOLD = (
    "Fold the sheet in half from top to bottom with the printed side out, then in half again with the photo "
    "on the front."
)
EXPECTED = 'expected {"greeting": "...", "verse": "..."}'

DEFAULT_CARD = Card(
    greeting="Best Wishes",
    verse=(
        "A smile caught in a single frame,\n"
        "a moment no one else can claim.\n\n"
        "May all the days ahead be bright,\n"
        "and full of friends, and warm, and light."
    ),
    signature="With love",
    back="Made at the prt photo booth",
)


def _clean_verse(text: str) -> str:
    """Like ``layout._clean`` keeping newlines, except that a blank line is kept, once, as a stanza break."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    kept = "".join(c for c in text if c == "\n" or unicodedata.category(c)[0] != "C")
    lines: list[str] = []
    for raw in kept.split("\n"):
        line = " ".join(raw.split())
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip("\n")


def check_card(card: Card, exit_code: int) -> Card:
    """Normalise whitespace and enforce the required fields, length limits and printable characters."""
    clean = {}
    for name in FIELDS:
        raw = getattr(card, name)
        value = _clean_verse(raw) if name == "verse" else _clean(raw, keep_newlines=name in MULTILINE)
        if name in REQUIRED and not value:
            raise PrinterError(f"the card has an empty {name}", hint=f"give the {name} some text", exit_code=exit_code)
        if len(value) > LIMITS[name]:
            raise PrinterError(
                f"{name} is too long ({len(value)} characters, max {LIMITS[name]})",
                hint=f"shorten the {name} so it fits on the card",
                exit_code=exit_code,
            )
        unprintable = sorted({c for c in value if _glyphs(c) is None})
        if unprintable:
            shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
            raise PrinterError(
                f"{name} has characters the card font cannot print: {shown}",
                hint="write it in Latin letters (accents are printed without their marks)",
                exit_code=exit_code,
            )
        clean[name] = value
    return Card(**clean)


def load_card(path: Path) -> Card:
    """Read a card JSON file: {"greeting", "verse", "message"?, "signature"?, "back"?}; ``-`` reads stdin."""
    if str(path) == STDIN:
        name = kind = "card on stdin"
        content = read_stdin(MAX_CARD_FILE)
        too_big = len(content) > MAX_CARD_FILE
    else:
        if not path.is_file():
            raise PrinterError(f"card file not found: {path}", hint="check the path", exit_code=3)
        name, kind = f"card file {path.name}", "card file"
        too_big = path.stat().st_size > MAX_CARD_FILE
        content = b"" if too_big else path.read_bytes()
    if too_big:
        raise PrinterError(
            f"{name} is larger than {MAX_CARD_FILE // 1024} KiB",
            hint="a greeting card holds a greeting and a short verse",
            exit_code=3,
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8 JSON", exit_code=3) from exc
    return parse_card(text, name, kind)


def parse_card(text: str, name: str = "card", kind: str = "card") -> Card:
    """A card from its JSON text, checked as a card file is; ``name`` and ``kind`` say where it came from in
    error messages (e.g. ``card file card.json`` and ``card file``)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrinterError(
            f"{name} is not valid JSON: {exc.msg} (line {exc.lineno})", hint=EXPECTED, exit_code=3
        ) from exc
    if not isinstance(data, dict):
        raise PrinterError(f"{name} must hold a JSON object", hint=EXPECTED, exit_code=3)
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise PrinterError(
            f"unknown field(s) in {kind}: {', '.join(unknown)}",
            hint="allowed fields: " + ", ".join(FIELDS),
            exit_code=3,
        )
    missing = [field for field in REQUIRED if field not in data]
    if missing:
        raise PrinterError(f"{name} is missing {' and '.join(missing)}", hint=EXPECTED, exit_code=3)
    wrong = [field for field, value in data.items() if not isinstance(value, str)]
    if wrong:
        raise PrinterError(
            f"{kind} fields must be text: {', '.join(sorted(wrong))}",
            hint='quote the values, e.g. "signature": "With love, Jo"',
            exit_code=3,
        )
    return check_card(Card(**data), exit_code=3)


def with_overrides(base: Card, **changes: str | None) -> Card:
    """Apply command-line text over a card; ``None`` keeps the base value."""
    return check_card(replace(base, **{k: v for k, v in changes.items() if v is not None}), exit_code=2)


# --- the sheet ---------------------------------------------------------------------------------

PANEL_W, PANEL_H = W // 2, H // 2  # one A6 panel: a quarter of the A4 sheet
# Where each panel goes on the sheet: (column, row) and whether it is printed upside down.
PANELS = {
    "inside_right": (0, 0, True),
    "inside_left": (1, 0, True),
    "back": (0, 1, False),
    "front": (1, 1, False),
}
TICK = 40  # length of the fold marks at the edges of the sheet

PAPER = (253, 250, 243)
ROSE = (178, 46, 82)
GOLD = (204, 160, 58)
INK = (52, 40, 44)
SOFT = (120, 104, 108)
WHITE = (255, 255, 255)
GUIDE = (175, 175, 175)
INSET = 48  # text and photo keep this far from the panel's edges, clear of a fold that is a little off
TEXT_W = PANEL_W - 2 * INSET - 20


def impose(panels: dict[str, Image.Image]) -> Image.Image:
    """The A4 sheet with each upright ``PANEL_W`` x ``PANEL_H`` panel in its place, the top row turned upside
    down, and short fold marks at the ends of the two fold lines."""
    sheet = Image.new("RGB", (W, H), "white")
    for name, (col, row, flipped) in PANELS.items():
        panel = panels[name]
        if panel.size != (PANEL_W, PANEL_H):
            raise ValueError(f"panel {name} is {panel.size}, not {(PANEL_W, PANEL_H)}")
        sheet.paste(panel.transpose(Image.Transpose.ROTATE_180) if flipped else panel, (col * PANEL_W, row * PANEL_H))
    d = ImageDraw.Draw(sheet)
    for x0, y0, x1, y1 in (
        (0, PANEL_H, TICK, PANEL_H),
        (W - TICK, PANEL_H, W, PANEL_H),
        (PANEL_W, 0, PANEL_W, TICK),
        (PANEL_W, H - TICK, PANEL_W, H),
    ):
        d.line([(x0, y0), (x1, y1)], fill=GUIDE, width=2)
    return sheet


# --- the panels --------------------------------------------------------------------------------


def _panel() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """A blank panel in its double border."""
    panel = Image.new("RGB", (PANEL_W, PANEL_H), PAPER)
    d = ImageDraw.Draw(panel)
    d.rectangle([22, 22, PANEL_W - 23, PANEL_H - 23], outline=ROSE, width=3)
    d.rectangle([32, 32, PANEL_W - 33, PANEL_H - 33], outline=GOLD, width=1)
    return panel, d


def _flourish(d: ImageDraw.ImageDraw, y: int, half: int = 90) -> None:
    """A small star between two short rules, centred on the panel."""
    mid = PANEL_W // 2
    d.line([(mid - half, y), (mid - 18, y)], fill=GOLD, width=2)
    d.line([(mid + 18, y), (mid + half, y)], fill=GOLD, width=2)
    _star(d, mid, y, 11, GOLD)


def _fitted(text: str, size: int, min_size: int, width: int, max_lines: int):
    """``layout._fit`` on the printed form of ``text``, with the cut counted in the characters of ``text``."""
    font, size, lines, cut = _fit(_printable(text), size, min_size, width, max_lines)
    return font, size, lines, (_in_source(cut, text, _printable) if cut else None)


def _centred_block(
    d: ImageDraw.ImageDraw, top: int, bottom: int, lines: list[str], font, size: int, fill, bold: int = 0
) -> None:
    """Draw ``lines`` centred on the panel, and centred between ``top`` and ``bottom``."""
    y = top + max(0, (bottom - top - len(lines) * _lead(size)) // 2)
    _text_block(d, (PANEL_W - TEXT_W) // 2, y, lines, font, size, fill, width=TEXT_W, bold=bold)


PHOTO_BOX = (INSET + 24, INSET + 24, PANEL_W - INSET - 24, 600)


def _front(photo: Image.Image, card: Card, cut: Cuts) -> Image.Image:
    """The photo in a gold frame, the greeting under it and a flourish."""
    panel, d = _panel()
    d.rectangle(_grow(PHOTO_BOX, 12), fill=GOLD)
    d.rectangle(_grow(PHOTO_BOX, 6), fill=WHITE)
    _photo(panel, photo, PHOTO_BOX, mono=False)
    font, size, lines, cut_greeting = _fitted(card.greeting, 58, 32, TEXT_W, 2)
    _centred_block(d, PHOTO_BOX[3] + 24, PANEL_H - 100, lines, font, size, ROSE, bold=1)
    if cut_greeting:
        cut["greeting"] = cut_greeting
    _flourish(d, PANEL_H - 78)
    return panel


MESSAGE_SIZE = 30


def _inside_left(card: Card, cut: Cuts) -> Image.Image:
    """The message, centred; without one the panel is left blank to write on."""
    panel, d = _panel()
    if card.message:
        text = _printable(card.message)
        box = (INSET + 10, INSET + 40, PANEL_W - INSET - 10, PANEL_H - INSET - 40)
        spare = box[3] - box[1] - _columns_height(text, box[2] - box[0], 1, 0, MESSAGE_SIZE)
        if spare > 0:  # a message that fits at full size sits in the middle of the panel
            box = (box[0], box[1] + spare // 2, box[2], box[3])
        if message_cut := _columns(d, text, box, 1, 0, MESSAGE_SIZE, 18, INK, centre=True):
            cut["message"] = _in_source(message_cut, card.message, _printable)
    return panel


VERSE_SIZE, VERSE_MIN = 34, 18
VERSE_WHOLE = 26  # the smallest type a verse is set in to keep each of its lines on one row
VERSE_BOX = (INSET + 10, 140, PANEL_W - INSET - 10, 690)
SIGNATURE_GAP = 34  # between the verse and the signature
SIGNATURE_BOTTOM = PANEL_H - INSET - 40  # the verse and the signature are centred above this


def _verse_rows(lines: list[str], font, width: int) -> list[tuple[int, str | None]]:
    """The rows of the verse: (line number, text), with None as the text of a gap between two stanzas."""
    rows: list[tuple[int, str | None]] = []
    for i, line in enumerate(lines):
        rows += [(i, None)] if not line else [(i, row) for row in _wrap(line, font, width)]
    return rows


def _rows_height(rows: list[tuple[int, str | None]], size: int) -> int:
    return sum(_lead(size) if text is not None else _lead(size) // 2 for _, text in rows)


def fit_verse(verse: str, width: int, height: int, size: int = VERSE_SIZE, min_size: int = VERSE_MIN):
    """The type that sets ``verse`` in a box of ``width`` x ``height``, each line of the verse on its own row(s) and
    half a row between stanzas: the largest size down to ``VERSE_WHOLE`` at which no line has to wrap, else the
    largest down to ``min_size`` at which the verse fits; cut with "…" at the smallest size.

    Returns (font, size, rows, cut): ``rows`` as from ``_verse_rows``, and ``cut`` None when the verse fits, else
    the ``Cut`` counted in the characters of ``verse`` (a cleaned verse, its newlines included), so the verse cut to
    its first ``max_fit`` characters fits.
    """
    printed = _printable(verse).split("\n")
    fitting = None
    for s in range(size, min_size - 1, -2):
        font = _font(s)
        rows = _verse_rows(printed, font, width)
        if _rows_height(rows, s) <= height:
            if len(rows) == len(printed) or s <= VERSE_WHOLE:  # every line on one row, or wrapping must do
                return font, s, rows, None
            fitting = fitting or (font, s, rows, None)
    if fitting:
        return fitting
    kept: list[tuple[int, str | None]] = []
    for row in rows:
        if _rows_height(kept + [row], s) > height:
            break
        kept.append(row)
    while kept and kept[-1][1] is None:  # end on a line of verse, not on a stanza gap
        kept.pop()
    if not kept:
        return font, s, [], Cut(0, len(verse))
    last, text = kept[-1]
    shown = _ellipsize(text, font, width)
    kept[-1] = (last, shown)
    # How much of the last line was printed, in the printed form of that line, then in its own characters.
    done = [t for i, t in kept[:-1] if i == last] + [shown.removesuffix("…").rstrip()]
    source = verse.split("\n")
    in_line = _in_source(Cut(_consumed(printed[last], done), 0), source[last], _printable).max_fit
    return font, s, kept, Cut(sum(len(line) + 1 for line in source[:last]) + in_line, len(verse))


def _inside_right(card: Card, cut: Cuts) -> Image.Image:
    """The verse under a flourish, and the signature under it, centred together on the panel."""
    panel, d = _panel()
    _flourish(d, 100)
    x0, y0, x1, y1 = VERSE_BOX
    font, size, rows, verse_cut = fit_verse(card.verse, x1 - x0, y1 - y0)
    if verse_cut:
        cut["verse"] = verse_cut
    signature = _fitted(card.signature, 30, 20, TEXT_W, 2) if card.signature else None
    below = SIGNATURE_GAP + len(signature[2]) * _lead(signature[1]) if signature else 0
    y = y0 + max(0, (SIGNATURE_BOTTOM - y0 - _rows_height(rows, size) - below) // 2)
    for _, text in rows:
        if text is not None:
            d.text(((PANEL_W - font.getlength(text)) / 2, y), text, font=font, fill=INK)
        y += _lead(size) if text is not None else _lead(size) // 2
    if signature:
        font, size, lines, cut_signature = signature
        _text_block(d, (PANEL_W - TEXT_W) // 2, y + SIGNATURE_GAP, lines, font, size, ROSE, width=TEXT_W, bold=1)
        if cut_signature:
            cut["signature"] = cut_signature
    return panel


def _back(card: Card, cut: Cuts) -> Image.Image:
    """A small emblem low on the panel, with the back line under it."""
    panel, d = _panel()
    _flourish(d, PANEL_H - 200, half=60)
    if card.back:
        font, size, lines, cut_back = _fitted(card.back, 22, 16, TEXT_W, 2)
        _centred_block(d, PANEL_H - 180, PANEL_H - INSET - 30, lines, font, size, SOFT)
        if cut_back:
            cut["back"] = cut_back
    return panel


def panels(photo: Image.Image, card: Card | None = None) -> tuple[dict[str, Image.Image], Cuts]:
    """Each panel drawn upright, by name (see ``PANELS``), and the fields cut with "…" to fit, in the order the
    card is read: front, inside left, inside right, back."""
    card = DEFAULT_CARD if card is None else card
    cut: Cuts = {}
    drawn = {
        "front": _front(photo, card, cut),
        "inside_left": _inside_left(card, cut),
        "inside_right": _inside_right(card, cut),
        "back": _back(card, cut),
    }
    return drawn, cut


def compose(photo: Image.Image, card: Card | None = None) -> tuple[Image.Image, Cuts]:
    """Lay out the sheet (with the default card if none is given); also returns the fields that were cut."""
    drawn, cut = panels(photo, card)
    return impose(drawn), cut
