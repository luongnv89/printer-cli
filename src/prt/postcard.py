"""Postcard: a photo on the front, and a message, address lines and a stamp box on the back, on 4×6 photo paper.

Both sides are laid out landscape on :data:`prt.paper.PHOTO_4X6` (1800 x 1200 px at 300 dpi) and print from the
photo tray. The front is the photo in a thin white edge, with an optional greeting written across its foot. The
back is a classic postcard back: "POSTCARD" over a centre divider, the message and the signature on the left, and
on the right a stamp box and four ruled address lines. The text is set in the bundled Caveat handwriting font;
letters it lacks print without their accents, and text it cannot print at all, such as emoji or Chinese, is refused
before anything is drawn.

A postcard is printed on both sides of one card, in one of two ways (see :func:`print_method`):

- **one two-sided job**, when the printer reports two-sided printing and its duplexer can take the card
  (``Paper.duplex``), or when asked for with ``--duplex``. The two sides go to the printer's CUPS queue as one
  two-page PDF flipped on the short edge, so the back reads the right way up when the card is turned over like a
  book page.
- **a guided manual flip** otherwise: the front is printed, the card is put back in the tray turned over (blank side
  where the photo side was, the same edge going in first) and the back is printed. Turned over that way the card
  flips about its long edge, so the back is sent turned a half turn (:func:`flipped`), and it reads the right way
  up when the card is turned over like a book page, as with the duplex job.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from prt import fonts
from prt.booth import STDIN, read_stdin
from prt.drivers.base import PrinterError
from prt.layout import Cut, Cuts, _clean, _cut, _ellipsize, _in_source, _paragraphs
from prt.paper import PHOTO_4X6, Paper
from prt.vintage import _char_in, _fit_face, _printable_face

PAPER = PHOTO_4X6
W, H = PAPER.landscape_size  # 1800 x 1200: a postcard is read on its side
KIND = "postcard"
SIDES = ("front", "back")

# --- the postcard text -------------------------------------------------------------------------


@dataclass(frozen=True)
class Postcard:
    greeting: str = ""
    message: str = ""
    signature: str = ""
    address: str = ""


FIELDS = tuple(f.name for f in fields(Postcard))
REQUIRED = ("message",)
# Upper bounds that keep the text on its side of the card; text that still overflows its space ends in "…", and
# compose() names the fields that were cut, with how much of each was printed, as prt booth does.
LIMITS = {"greeting": 60, "message": 500, "signature": 60, "address": 160}
ADDRESS_LINES = 4  # ruled lines on the back; each line of the address is written on one of them
MULTILINE = ("message", "address")  # a newline starts a paragraph of the message, a line of the address
MAX_POSTCARD_FILE = 64 * 1024
EXPECTED = 'expected {"message": "..."}'
FLIP = ("Put the card back in the photo tray turned over, the blank side facing the way the photo side did, with "
        "the same edge going in first.")

DEFAULT_POSTCARD = Postcard(
    greeting="Greetings from afar",
    message=("The weather is lovely, the food is even better and the view is just like the picture on the "
             "front.\nWish you were here!"),
    signature="See you soon",
)


def check_postcard(card: Postcard, exit_code: int) -> Postcard:
    """Normalise whitespace and enforce the required fields, length limits, address lines and printable
    characters."""
    face = fonts.face("hand", 40)
    clean = {}
    for name in FIELDS:
        value = _clean(getattr(card, name), keep_newlines=name in MULTILINE)
        if name in REQUIRED and not value:
            raise PrinterError(f"the postcard has an empty {name}", hint=f"give the {name} some text",
                               exit_code=exit_code)
        if len(value) > LIMITS[name]:
            raise PrinterError(f"{name} is too long ({len(value)} characters, max {LIMITS[name]})",
                               hint=f"shorten the {name} so it fits on the postcard", exit_code=exit_code)
        if name == "address" and value.count("\n") >= ADDRESS_LINES:
            raise PrinterError(f"the address has {value.count(chr(10)) + 1} lines, max {ADDRESS_LINES}",
                               hint=f"write the address on at most {ADDRESS_LINES} lines", exit_code=exit_code)
        unprintable = sorted({c for c in value if c not in "?\n" and _char_in(face, c) == "?"})
        if unprintable:
            shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
            raise PrinterError(f"{name} has characters the handwriting font cannot print: {shown}",
                               hint="write it in Latin, Greek or Cyrillic letters (no emoji)", exit_code=exit_code)
        clean[name] = value
    return Postcard(**clean)


def load_postcard(path: Path) -> Postcard:
    """Read a postcard JSON file: {"message", "greeting"?, "signature"?, "address"?}; ``-`` reads stdin."""
    if str(path) == STDIN:
        name = kind = "postcard on stdin"
        content = read_stdin(MAX_POSTCARD_FILE)
        too_big = len(content) > MAX_POSTCARD_FILE
    else:
        if not path.is_file():
            raise PrinterError(f"postcard file not found: {path}", hint="check the path", exit_code=3)
        name, kind = f"postcard file {path.name}", "postcard file"
        too_big = path.stat().st_size > MAX_POSTCARD_FILE
        content = b"" if too_big else path.read_bytes()
    if too_big:
        raise PrinterError(f"{name} is larger than {MAX_POSTCARD_FILE // 1024} KiB",
                           hint="a postcard holds a short message and an address", exit_code=3)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8 JSON", exit_code=3) from exc
    return parse_postcard(text, name, kind)


def parse_postcard(text: str, name: str = "postcard", kind: str = "postcard") -> Postcard:
    """A postcard from its JSON text, checked as a postcard file is; ``name`` and ``kind`` say where it came from
    in error messages (e.g. ``postcard file card.json`` and ``postcard file``)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrinterError(f"{name} is not valid JSON: {exc.msg} (line {exc.lineno})", hint=EXPECTED,
                           exit_code=3) from exc
    if not isinstance(data, dict):
        raise PrinterError(f"{name} must hold a JSON object", hint=EXPECTED, exit_code=3)
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise PrinterError(f"unknown field(s) in {kind}: {', '.join(unknown)}",
                           hint="allowed fields: " + ", ".join(FIELDS), exit_code=3)
    missing = [field for field in REQUIRED if field not in data]
    if missing:
        raise PrinterError(f"{name} is missing {' and '.join(missing)}", hint=EXPECTED, exit_code=3)
    wrong = [field for field, value in data.items() if not isinstance(value, str)]
    if wrong:
        raise PrinterError(f"{kind} fields must be text: {', '.join(sorted(wrong))}",
                           hint='quote the values, e.g. "address": "Jo Smith\\n12 High Street"', exit_code=3)
    return check_postcard(Postcard(**data), exit_code=3)


def with_overrides(base: Postcard, **changes: str | None) -> Postcard:
    """Apply command-line text over a postcard; ``None`` keeps the base value."""
    return check_postcard(replace(base, **{k: v for k, v in changes.items() if v is not None}), exit_code=2)


# --- the front ---------------------------------------------------------------------------------

EDGE = 48  # the white edge round the photo (4 mm)
CARD = (252, 251, 246)  # the white of the card
PHOTO_BOX = (EDGE, EDGE, W - EDGE, H - EDGE)
GREETING_PAD = 36  # inside the greeting's band, above, below and either side of the text
BAND = (16, 18, 28, 120)  # the see-through dark band the greeting is written on, so it reads on any photo
GREETING_SIZE, GREETING_MIN = 132, 80  # on one line; a greeting too long for it goes on two, from 72 down to 52


def fit_greeting(text: str, width: int):
    """The greeting in handwriting: on one line from 132 px down to 80, else on two lines from 72 px down to 52,
    cut with "…" at the smallest (see ``vintage._fit_face``)."""
    fitted = _fit_face(text, "hand-bold", GREETING_SIZE, GREETING_MIN, width, 1)
    return fitted if fitted[3] is None else _fit_face(text, "hand-bold", 72, 52, width, 2)


def front(photo: Image.Image, card: Postcard, cut: Cuts) -> Image.Image:
    """The photo, cropped to fill the card inside a thin white edge, and the greeting written across its foot on
    a see-through band (none without a greeting)."""
    page = Image.new("RGB", (W, H), CARD)
    x0, y0, x1, y1 = PHOTO_BOX
    fitted = ImageOps.fit(photo.convert("RGB"), (x1 - x0, y1 - y0), method=Image.Resampling.LANCZOS,
                          centering=(0.5, 0.4))
    page.paste(fitted, (x0, y0))
    if card.greeting:
        font, size, lines, cut_greeting = fit_greeting(card.greeting, x1 - x0 - 2 * GREETING_PAD)
        if cut_greeting:
            cut["greeting"] = cut_greeting
        lead = round(size * 1.1)
        band_h = lead * len(lines) + 2 * GREETING_PAD
        band = Image.new("RGBA", (x1 - x0, band_h), BAND)
        page.paste(band, (x0, y1 - band_h), band)
        d = ImageDraw.Draw(page)
        y = y1 - band_h + GREETING_PAD
        for line in lines:
            d.text(((x0 + x1) / 2, y + lead * 0.8), line, font=font, fill=(255, 255, 255), anchor="ms")
            y += lead
    return page


# --- the back ----------------------------------------------------------------------------------

MARGIN = 60
DIVIDER_X = 960  # the line between the message (left) and the address (right)
PRINTED = (110, 106, 100)  # the parts a postcard back comes printed with: heading, divider, stamp box, lines
PEN = (30, 38, 78)  # blue-black ink, as the instant photo's caption
STAMP_BOX = (W - MARGIN - 250, MARGIN + 30, W - MARGIN, MARGIN + 330)  # 21 x 25 mm
ADDRESS_LEFT, ADDRESS_RIGHT = DIVIDER_X + 60, W - MARGIN - 20
ADDRESS_BASELINES = (620, 750, 880, 1010)  # the ruled address lines, from the top
ADDRESS_SIZE, ADDRESS_MIN = 64, 36
MESSAGE_BOX = (MARGIN + 50, 190, DIVIDER_X - 60, 1000)
SIGNATURE_BOX = (MARGIN + 50, 1010, DIVIDER_X - 60, H - MARGIN - 20)
MESSAGE_SIZE, MESSAGE_MIN = 64, 36
SIGNATURE_SIZE, SIGNATURE_MIN = 60, 40


def _lead(size: int) -> int:
    return round(size * 1.25)


def _height(lines: list[str], size: int) -> int:
    """How tall ``lines`` are written at ``size``: a full row each, half a row for the gap between paragraphs."""
    return sum(_lead(size) if line else _lead(size) // 2 for line in lines)


def fit_message(text: str, width: int, height: int, size: int = MESSAGE_SIZE, min_size: int = MESSAGE_MIN):
    """The handwriting that sets ``text`` (paragraphs split by newlines) in a box of ``width`` x ``height``: the
    largest size down to ``min_size`` at which it fits, cut with "…" at the smallest.

    Returns (font, size, lines, cut): ``lines`` as from ``layout._paragraphs`` ("" between two paragraphs), and
    ``cut`` None when the text fits, else the ``Cut`` counted in the characters of ``text``.
    """
    for s in range(size, min_size - 1, -2):
        font = fonts.face("hand", s)
        printable = "\n".join(_printable_face(para, font) for para in text.split("\n"))  # keeps the newlines
        lines = _paragraphs(printable, font, width)
        if _height(lines, s) <= height:
            return font, s, lines, None
    kept: list[str] = []
    for line in lines:
        if _height(kept + [line], s) > height:
            break
        kept.append(line)
    while kept and not kept[-1]:  # end on a line of text, not on a paragraph gap
        kept.pop()
    if not kept:
        return font, s, [], Cut(0, len(text))
    kept[-1] = _ellipsize(kept[-1], font, width)
    return font, s, kept, _in_source(_cut(printable, kept), text, lambda c: _printable_face(c, font))


def _spaced(d: ImageDraw.ImageDraw, centre: float, baseline: int, text: str, size: int, spacing: int) -> None:
    """``text`` in letter-spaced capitals, centred on ``centre``."""
    font = fonts.face("body", size)
    width = sum(font.getlength(c) for c in text) + spacing * (len(text) - 1)
    x = centre - width / 2
    for c in text:
        d.text((x, baseline), c, font=font, fill=PRINTED, anchor="ls")
        x += font.getlength(c) + spacing


def _address(d: ImageDraw.ImageDraw, address: str, cut: Cuts) -> None:
    """The four ruled address lines, each line of ``address`` written on one; a line too long for its rule is
    cut with "…", and the address is then named in ``cut`` with how much of it fits."""
    lines = address.split("\n") if address else []
    done = 0  # characters of the address before the line being written, its newline included
    for i, baseline in enumerate(ADDRESS_BASELINES):
        d.line([(ADDRESS_LEFT, baseline), (ADDRESS_RIGHT, baseline)], fill=PRINTED, width=2)
        if i >= len(lines):
            continue
        font, _, shown, line_cut = _fit_face(lines[i], "hand", ADDRESS_SIZE, ADDRESS_MIN,
                                             ADDRESS_RIGHT - ADDRESS_LEFT - 16, 1)
        d.text((ADDRESS_LEFT + 8, baseline - 14), shown[0] if shown else "", font=font, fill=PEN, anchor="ls")
        if line_cut and "address" not in cut:
            cut["address"] = Cut(done + line_cut.max_fit, len(address))
        done += len(lines[i]) + 1


def back(card: Postcard, cut: Cuts) -> Image.Image:
    """The back: "POSTCARD" over the divider, the message and the signature on the left, the stamp box and the
    address lines on the right."""
    page = Image.new("RGB", (W, H), CARD)
    d = ImageDraw.Draw(page)
    _spaced(d, DIVIDER_X, MARGIN + 50, "POSTCARD", 44, 18)
    d.line([(DIVIDER_X, 170), (DIVIDER_X, H - MARGIN - 30)], fill=PRINTED, width=3)
    d.rectangle(STAMP_BOX, outline=PRINTED, width=3)
    sx0, sy0, sx1, sy1 = STAMP_BOX
    for n, word in enumerate(("PLACE", "STAMP", "HERE")):
        _spaced(d, (sx0 + sx1) / 2, (sy0 + sy1) // 2 - 30 + n * 40, word, 26, 4)
    x0, y0, x1, y1 = MESSAGE_BOX
    bottom = y1 if card.signature else SIGNATURE_BOX[3]
    font, size, lines, message_cut = fit_message(card.message, x1 - x0, bottom - y0)
    if message_cut:
        cut["message"] = message_cut
    y = y0
    for line in lines:
        if line:
            d.text((x0, y + round(size * 0.95)), line, font=font, fill=PEN, anchor="ls")
        y += _lead(size) if line else _lead(size) // 2
    if card.signature:
        sx0, sy0, sx1, sy1 = SIGNATURE_BOX
        font, size, shown, signature_cut = _fit_face(card.signature, "hand", SIGNATURE_SIZE, SIGNATURE_MIN,
                                                     sx1 - sx0, 1)
        d.text((sx1, sy1 - 20), shown[0] if shown else "", font=font, fill=PEN, anchor="rs")
        if signature_cut:
            cut["signature"] = signature_cut
    _address(d, card.address, cut)
    return page


def compose(photo: Image.Image, card: Postcard | None = None) -> tuple[list[Image.Image], Cuts]:
    """Both sides, front then back, each upright as the postcard is read (landscape), and the fields cut with "…"
    to fit, in the order the card is read: greeting, message, signature, address. Uses the default postcard when
    none is given."""
    card = DEFAULT_POSTCARD if card is None else card
    cut: Cuts = {}
    return [front(photo, card, cut), back(card, cut)], cut


def flipped(page: Image.Image) -> Image.Image:
    """The back as it is sent for a manual flip: a half turn, since the card turned over with the same edge
    going in first flips about its long edge (see the module docstring)."""
    return page.transpose(Image.Transpose.ROTATE_180)


# --- how the two sides are printed ---------------------------------------------------------------


def side_paths(path: Path, fmt: str) -> list[Path]:
    """The files a preview or copy of both sides is written to: one two-page PDF, or ``NAME-front`` and
    ``NAME-back`` images."""
    if fmt == "pdf":
        return [path]
    return [path.with_name(f"{path.stem}-{side}{path.suffix}") for side in SIDES]


def check_side(side: str, duplex: bool) -> None:
    """``--duplex`` prints both sides, so it cannot go with one side alone (exit code 2)."""
    if duplex and side != "both":
        raise PrinterError(f"--duplex prints both sides, it cannot go with --side {side}",
                           hint="drop --side, or drop --duplex", exit_code=2)


def print_method(side: str, duplex: bool, paper: Paper, two_sided: Callable[[], bool],
                 queue: Callable[[], str | None], interactive: bool) -> tuple[str, str | None]:
    """How to print ``side`` ("front", "back" or "both"): returns (method, CUPS queue).

    The method is ``front`` or ``back`` (that side alone), ``duplex`` (both sides in one two-sided job through the
    returned queue) or ``flip`` (the front, then a prompt to turn the card over, then the back). ``duplex`` asks
    for the two-sided job; without it one is used only when the printer reports two-sided printing
    (``two_sided``), its duplexer takes the paper (``paper.duplex``) and the printer has a CUPS queue (``queue``).
    ``two_sided`` and ``queue`` ask the printer and the system, so they are called only when needed. Both sides
    without a two-sided job need someone to turn the card over: without a terminal (``interactive`` false, as in
    agent mode) that is refused, exit code 2, before anything is sent.
    """
    check_side(side, duplex)
    if side != "both":
        return side, None
    if duplex:
        if not two_sided():
            raise PrinterError("the printer does not report two-sided printing",
                               hint="drop --duplex to turn the card over by hand", kind="duplex_unavailable")
        found = queue()
        if not found:
            raise PrinterError("a two-sided postcard goes through the printer's CUPS queue, and none was found",
                               hint="add the printer in System Settings → Printers (or `lpadmin`), or drop "
                                    "--duplex to turn the card over by hand", kind="duplex_unavailable")
        return "duplex", found
    if paper.duplex and two_sided():
        found = queue()
        if found:
            return "duplex", found
    if not interactive:
        raise PrinterError("printing both sides means turning the card over between them, and there is no "
                           "terminal to say when", exit_code=2, kind="manual_flip_required",
                           hint="print one side at a time: --side front, turn the card over, then --side back")
    return "flip", None
