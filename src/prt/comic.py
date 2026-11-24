"""Comic strip: 3 to 6 photos as the panels of a comic page, with speech bubbles, thought bubbles and captions.

The page is A4, upright, at 150 dpi (:data:`prt.paper.A4`): an optional title and byline at the top, then the
panels in rows (:data:`ROWS`), each photo cropped to fill its panel inside a black border. Every panel may carry a
narration caption (a yellow box in its top-left corner) and up to :data:`MAX_BUBBLES` bubbles. A bubble sits in the
top or bottom band of its panel, at the left, in the middle or at the right (``position``), with a tail that points
down from a top bubble (up from a bottom one) towards the left, the middle or the right (``tail``); a ``thought``
bubble is a cloud with a trail of small puffs. Bubbles in the same band share its width, in the order of their
positions from left to right.

The words come from a JSON comic file (see :func:`parse_comic`, ``prt schema comic``), one panel per photo, in
order. Each bubble and caption is set in the bundled Caveat handwriting font, as large as fits its space; text that
still does not fit at the smallest size ends in "…", and :func:`compose` names it in the fit report as
``panels[i].bubbles[j]`` or ``panels[i].caption`` with how many of its characters were printed, as ``prt booth``
does for its fields. Letters the font lacks print without their accents, and text it cannot print at all, such as
emoji or Chinese, is refused before anything is drawn.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from prt import fonts
from prt.booth import STDIN, _photo, read_stdin
from prt.drivers.base import PrinterError
from prt.layout import Cut, Cuts, _clean, _wrap
from prt.paper import A4
from prt.vintage import _char_in, _fit_face, _printable_face

PAPER = A4
W, H = PAPER.size  # 1240 x 1754
KIND = "comic strip"

MIN_PANELS, MAX_PANELS = 3, 6
MAX_BUBBLES = 3  # in one panel
# The panels in each row, from the top, for each number of panels: a wide panel opens a strip of 3 or 5.
ROWS = {3: (1, 2), 4: (2, 2), 5: (2, 1, 2), 6: (2, 2, 2)}

# --- the comic file ----------------------------------------------------------------------------

POSITIONS = ("top-left", "top", "top-right", "bottom-left", "bottom", "bottom-right")
TAILS = ("left", "center", "right", "none")
BUBBLE_KINDS = ("speech", "thought")
# Where a bubble goes when its position is not given: the first two side by side at the top, the third at the
# bottom right.
DEFAULT_POSITIONS = ("top-left", "top-right", "bottom-right")
LIMITS = {"title": 60, "byline": 60, "caption": 100, "text": 140}
MAX_COMIC_FILE = 64 * 1024
EXPECTED = 'expected {"panels": [{"bubbles": [{"text": "..."}]}, ...]}'


@dataclass(frozen=True)
class Bubble:
    """One bubble: its words, which band and side of the panel it sits in, where its tail points and its shape."""
    text: str
    position: str = ""  # one of POSITIONS; "" takes DEFAULT_POSITIONS by the bubble's place in its panel
    tail: str = "center"  # one of TAILS
    kind: str = "speech"  # one of BUBBLE_KINDS


@dataclass(frozen=True)
class Panel:
    """One panel: the bubbles over its photo, and an optional narration caption."""
    bubbles: tuple[Bubble, ...] = ()
    caption: str = ""


@dataclass(frozen=True)
class Comic:
    panels: tuple[Panel, ...]
    title: str = ""
    byline: str = ""


COMIC_FIELDS = tuple(f.name for f in fields(Comic))
PANEL_FIELDS = tuple(f.name for f in fields(Panel))
BUBBLE_FIELDS = tuple(f.name for f in fields(Bubble))

DEFAULT_COMIC = Comic(
    title="The Big Day",
    byline="A prt comic",
    panels=(
        Panel(caption="It started like any other morning...",
              bubbles=(Bubble("Is the printer even on?"),)),
        Panel(bubbles=(Bubble("It says the ink is fine!"),
                       Bubble("Too easy. Something is up.", position="top-right", kind="thought"))),
        Panel(caption="Moments later",
              bubbles=(Bubble("Wait... is that us on the page?", position="top"),)),
        Panel(bubbles=(Bubble("We look amazing!", position="bottom-left"),)),
        Panel(bubbles=(Bubble("Print another one!", tail="left"),)),
        Panel(caption="The end... or is it?", bubbles=(Bubble("Same time tomorrow?", position="top-right"),)),
    ),
)


def to_dict(comic: Comic) -> dict[str, Any]:
    """The comic as a comic file holds it, leaving out what is empty or the default (``prt schema comic``'s
    example)."""
    def bubble(b: Bubble) -> dict[str, str]:
        out = {"text": b.text}
        out.update({k: v for k, v, default in (("position", b.position, ""), ("tail", b.tail, "center"),
                                                ("kind", b.kind, "speech")) if v != default})
        return out

    def panel(p: Panel) -> dict[str, Any]:
        out: dict[str, Any] = {"caption": p.caption} if p.caption else {}
        return {**out, "bubbles": [bubble(b) for b in p.bubbles]} if p.bubbles else out
    head = {k: v for k, v in (("title", comic.title), ("byline", comic.byline)) if v}
    return {**head, "panels": [panel(p) for p in comic.panels]}


def default_comic(panels: int) -> Comic:
    """The built-in comic cut to ``panels`` panels: its first ones and, last, its punchline."""
    check_count(panels)
    return check_comic(replace(DEFAULT_COMIC, panels=DEFAULT_COMIC.panels[:panels - 1] + DEFAULT_COMIC.panels[-1:]),
                       exit_code=2)


def check_count(n: int) -> None:
    """A comic takes :data:`MIN_PANELS` to :data:`MAX_PANELS` photos (exit code 2 otherwise)."""
    if not MIN_PANELS <= n <= MAX_PANELS:
        raise PrinterError(f"a comic strip takes {MIN_PANELS} to {MAX_PANELS} photos, got {n}",
                           hint="give one photo per panel, in reading order (repeat one to use it twice)",
                           exit_code=2)


def check_panels(comic: Comic, photos: int) -> None:
    """The comic must have one panel per photo (exit code 2 otherwise)."""
    if len(comic.panels) != photos:
        raise PrinterError(f"the comic has {len(comic.panels)} panels but {photos} photos were given",
                           hint="give one photo per panel, in the order of the panels", exit_code=2)


def _printable_check(where: str, value: str, exit_code: int) -> None:
    face = fonts.face("hand", 40)
    unprintable = sorted({c for c in value if c != "?" and _char_in(face, c) == "?"})
    if unprintable:
        shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
        raise PrinterError(f"{where} has characters the handwriting font cannot print: {shown}",
                           hint="write it in Latin or Cyrillic letters (no emoji)", exit_code=exit_code)


def _text(where: str, raw: str, limit: int, exit_code: int, required: bool = False) -> str:
    value = _clean(raw, keep_newlines=False)
    if required and not value:
        raise PrinterError(f"{where} is empty", hint="give it some text, or leave the bubble out",
                           exit_code=exit_code)
    if len(value) > limit:
        raise PrinterError(f"{where} is too long ({len(value)} characters, max {limit})",
                           hint=f"shorten it to {limit} characters or fewer", exit_code=exit_code)
    _printable_check(where, value, exit_code)
    return value


def check_comic(comic: Comic, exit_code: int) -> Comic:
    """Normalise whitespace, fill in each bubble's default position, and enforce the number of panels and bubbles,
    the known positions, tails and kinds, the length limits and printable characters."""
    if not MIN_PANELS <= len(comic.panels) <= MAX_PANELS:
        raise PrinterError(f"the comic has {len(comic.panels)} panels; a comic strip has {MIN_PANELS} to "
                           f"{MAX_PANELS}", hint="give one panel per photo", exit_code=exit_code)
    panels = []
    for i, panel in enumerate(comic.panels):
        if len(panel.bubbles) > MAX_BUBBLES:
            raise PrinterError(f"panels[{i}] has {len(panel.bubbles)} bubbles, more than {MAX_BUBBLES}",
                               hint=f"keep to {MAX_BUBBLES} bubbles a panel", exit_code=exit_code)
        bubbles = []
        for j, bubble in enumerate(panel.bubbles):
            where = f"panels[{i}].bubbles[{j}]"
            position = bubble.position or DEFAULT_POSITIONS[j]
            for name, value, allowed in (("position", position, POSITIONS), ("tail", bubble.tail, TAILS),
                                         ("kind", bubble.kind, BUBBLE_KINDS)):
                if value not in allowed:
                    raise PrinterError(f"{where}.{name} must be one of {', '.join(allowed)}, not {value!r}",
                                       hint=EXPECTED, exit_code=exit_code)
            bubbles.append(Bubble(_text(f"{where}.text", bubble.text, LIMITS["text"], exit_code, required=True),
                                  position, bubble.tail, bubble.kind))
        caption = _text(f"panels[{i}].caption", panel.caption, LIMITS["caption"], exit_code)
        panels.append(Panel(tuple(bubbles), caption))
    return Comic(tuple(panels), _text("title", comic.title, LIMITS["title"], exit_code),
                 _text("byline", comic.byline, LIMITS["byline"], exit_code))


def load_comic(path: Path) -> Comic:
    """Read a comic JSON file (see :func:`parse_comic`); ``-`` reads stdin."""
    if str(path) == STDIN:
        name = "comic on stdin"
        content = read_stdin(MAX_COMIC_FILE)
        too_big = len(content) > MAX_COMIC_FILE
    else:
        if not path.is_file():
            raise PrinterError(f"comic file not found: {path}", hint="check the path", exit_code=3)
        name = f"comic file {path.name}"
        too_big = path.stat().st_size > MAX_COMIC_FILE
        content = b"" if too_big else path.read_bytes()
    if too_big:
        raise PrinterError(f"{name} is larger than {MAX_COMIC_FILE // 1024} KiB",
                           hint="a comic holds a few short lines a panel", exit_code=3)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8 JSON", exit_code=3) from exc
    return parse_comic(text, name)


def _object(value: Any, where: str, allowed: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PrinterError(f"{where} must be a JSON object", hint=EXPECTED, exit_code=3)
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise PrinterError(f"unknown field(s) in {where}: {', '.join(unknown)}",
                           hint="allowed fields: " + ", ".join(allowed), exit_code=3)
    wrong = sorted(k for k, v in value.items() if k not in ("panels", "bubbles") and not isinstance(v, str))
    if wrong:
        raise PrinterError(f"{where}: {', '.join(wrong)} must be text", hint='quote the values, e.g. "text": "Hi!"',
                           exit_code=3)
    return value


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise PrinterError(f"{where} must be a list", hint=EXPECTED, exit_code=3)
    return value


def parse_comic(text: str, name: str = "comic") -> Comic:
    """A comic from its JSON text: ``{"title"?, "byline"?, "panels": [{"caption"?, "bubbles"?: [{"text",
    "position"?, "tail"?, "kind"?}]}]}``, checked as a comic file is (exit code 3 on any fault, the message saying
    where, e.g. ``panels[1].bubbles[0].position``); ``name`` says where it came from in error messages."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrinterError(f"{name} is not valid JSON: {exc.msg} (line {exc.lineno})", hint=EXPECTED,
                           exit_code=3) from exc
    if not isinstance(data, dict):
        raise PrinterError(f"{name} must hold a JSON object", hint=EXPECTED, exit_code=3)
    data = _object(data, name, COMIC_FIELDS)
    if "panels" not in data:
        raise PrinterError(f"{name} is missing panels", hint=EXPECTED, exit_code=3)
    panels = []
    for i, raw in enumerate(_list(data["panels"], "panels")):
        panel = _object(raw, f"panels[{i}]", PANEL_FIELDS)
        bubbles = []
        for j, item in enumerate(_list(panel.get("bubbles", []), f"panels[{i}].bubbles")):
            bubble = _object(item, f"panels[{i}].bubbles[{j}]", BUBBLE_FIELDS)
            if "text" not in bubble:
                raise PrinterError(f"panels[{i}].bubbles[{j}] is missing text", hint=EXPECTED, exit_code=3)
            bubbles.append(Bubble(**bubble))
        panels.append(Panel(tuple(bubbles), panel.get("caption", "")))
    return check_comic(Comic(tuple(panels), data.get("title", ""), data.get("byline", "")), exit_code=3)


def with_overrides(base: Comic, title: str | None = None, byline: str | None = None) -> Comic:
    """Apply command-line text over a comic; ``None`` keeps the base value."""
    changes = {k: v for k, v in (("title", title), ("byline", byline)) if v is not None}
    return check_comic(replace(base, **changes), exit_code=2)


# --- the page ----------------------------------------------------------------------------------

MARGIN = 64  # round the panels: the printer cannot print to the edge of the sheet
GUTTER = 26  # between two panels
BORDER = 5  # the black line round a panel
INSET = 16  # bubbles and captions keep this far inside a panel's border
INK = (16, 16, 16)
WHITE = (255, 255, 255)
CAPTION_FILL = (255, 236, 150)
STROKE = 3  # a bubble's outline
TITLE_SIZE, TITLE_MIN = 92, 56
BYLINE_SIZE, BYLINE_MIN = 34, 24
CAPTION_SIZE, CAPTION_MIN = 30, 20
BUBBLE_SIZE, BUBBLE_MIN = 40, 22
PAD = 14  # between a bubble's words and its edge
KX = 1.25  # a speech bubble is this many times as wide as its words
KY = 1 / math.sqrt(1 - 1 / KX ** 2)  # ... and so this many times as tall, for the corners of the words to fit
SUPERSAMPLE = 3  # bubbles are drawn this many times larger, then shrunk, for smooth edges


def _lead(size: int) -> int:
    return round(size * 1.08)


def panel_boxes(n: int, top: int = MARGIN) -> list[tuple[int, int, int, int]]:
    """The box of each of ``n`` panels in reading order, in rows (see :data:`ROWS`) from ``top`` down to the foot of
    the page."""
    rows = ROWS[n]
    row_h = (H - MARGIN - top - GUTTER * (len(rows) - 1)) // len(rows)
    boxes = []
    for r, count in enumerate(rows):
        y0 = top + r * (row_h + GUTTER)
        width = (W - 2 * MARGIN - GUTTER * (count - 1)) // count
        boxes += [(MARGIN + c * (width + GUTTER), y0, MARGIN + c * (width + GUTTER) + width, y0 + row_h)
                  for c in range(count)]
    return boxes


def _head(d: ImageDraw.ImageDraw, comic: Comic, cut: Cuts) -> int:
    """The title and the byline, centred above the panels, over a rule; returns where the panels start."""
    if not comic.title and not comic.byline:
        return MARGIN
    y = MARGIN - 20
    width = W - 2 * MARGIN
    for name, role, size, min_size in (("title", "hand-bold", TITLE_SIZE, TITLE_MIN),
                                       ("byline", "hand", BYLINE_SIZE, BYLINE_MIN)):
        text = getattr(comic, name)
        if not text:
            continue
        font, size, lines, line_cut = _fit_face(text, role, size, min_size, width, 1)
        if line_cut:
            cut[name] = line_cut
        if lines:
            d.text((W / 2, y), lines[0], font=font, fill=INK, anchor="ma")
        y += _lead(size)
    y += 14
    d.line([(MARGIN, y), (W - MARGIN, y)], fill=INK, width=4)
    return y + 22


def _caption(d: ImageDraw.ImageDraw, text: str, x0: int, y0: int, width: int) -> tuple[int, Cut | None]:
    """The narration box in the top-left corner at (``x0``, ``y0``), as wide as its words; returns (its bottom,
    cut)."""
    font, size, lines, cut = _fit_face(text, "hand", CAPTION_SIZE, CAPTION_MIN, width - 2 * PAD, 2)
    if not lines:
        return y0, cut
    w = max(font.getlength(line) for line in lines) + 2 * PAD
    h = len(lines) * _lead(size) + PAD
    d.rectangle([x0, y0, x0 + w, y0 + h], fill=CAPTION_FILL, outline=INK, width=STROKE)
    for k, line in enumerate(lines):
        d.text((x0 + PAD, y0 + PAD // 2 + k * _lead(size)), line, font=font, fill=INK)
    return round(y0 + h), cut


@dataclass(frozen=True)
class Fitted:
    """A bubble's words laid out: the font and size, the lines, and the size of the shape round them."""
    font: Any
    size: int
    lines: list[str]
    width: float  # of the shape, without the tail
    height: float
    puff: float  # the radius of a thought bubble's puffs; 0 for speech
    cut: Cut | None


PUFF_MAX = 18  # the largest puff of a thought bubble


def _puff(b: float) -> float:
    return max(8.0, min(float(PUFF_MAX), b * 0.28))


def _shape(text_w: float, text_h: float, kind: str) -> tuple[float, float, float]:
    """The (width, height, puff radius) of a bubble round words ``text_w`` x ``text_h``."""
    a, b = text_w / 2 * KX + PAD, text_h / 2 * KY + PAD
    puff = _puff(b) if kind == "thought" else 0.0
    return 2 * (a + puff), 2 * (b + puff), puff


def _widest(text: str, font) -> int:
    """The width of the longest word of ``text`` as ``font`` prints it."""
    return max((math.ceil(font.getlength(word)) for word in _printable_face(text, font).split()), default=0)


def fit_bubble(text: str, kind: str, max_w: float, max_h: float) -> Fitted:
    """The largest type (``BUBBLE_SIZE`` down to ``BUBBLE_MIN``) at which ``text`` fits in a bubble at most
    ``max_w`` x ``max_h``, with the lines balanced so the bubble is no wider than it needs to be; cut with "…" at
    the smallest size, or left empty when not even one line fits."""
    extra = 2 * PUFF_MAX if kind == "thought" else 0  # the most a thought bubble's puffs add round it
    text_w = int((max_w - extra - 2 * PAD) / KX)
    fitted = split = None  # the last size tried, and the largest that fits only by splitting a long word
    for s in range(BUBBLE_SIZE, BUBBLE_MIN - 1, -2):
        rows = int((max_h - extra - 2 * PAD) / KY // _lead(s))
        if rows < 1 or text_w < s:
            continue
        font, s, lines, cut = _fit_face(text, "hand-bold", s, s, text_w, rows)
        fitted = font, s, lines, cut
        if cut is None:
            if _widest(text, font) <= text_w:
                break
            split = split or fitted
    else:
        fitted = split or fitted
    if fitted is None:
        return Fitted(fonts.face("hand-bold", BUBBLE_MIN), BUBBLE_MIN, [], 0, 0, 0, Cut(0, len(text)))
    font, s, lines, cut = fitted
    if cut is None and len(lines) > 1:  # balance: the narrowest width that keeps the same number of lines
        printable = _printable_face(text, font)
        lo, hi = min(text_w, max(text_w // 3, _widest(text, font))), text_w  # never split a word to balance
        while hi - lo > 4:
            mid = (lo + hi) // 2
            lo, hi = (lo, mid) if len(_wrap(printable, font, mid)) <= len(lines) else (mid, hi)
        lines = _wrap(printable, font, hi)
    width, height, puff = _shape(max(font.getlength(line) for line in lines), len(lines) * _lead(s), kind)
    return Fitted(font, s, lines, min(width, max_w), min(height, max_h), puff, cut)


def _ellipse_y(cx: float, cy: float, a: float, b: float, x: float, below: bool) -> float:
    """Where the ellipse's edge is at ``x``, on its lower (``below``) or upper side."""
    t = max(0.0, 1 - ((x - cx) / a) ** 2)
    return cy + (1 if below else -1) * b * math.sqrt(t)


def _draw_bubble(page: Image.Image, fitted: Fitted, box: tuple[float, float, float, float], kind: str,
                 tail: str, tip: tuple[float, float] | None, below: bool) -> None:
    """Draw the bubble filling ``box`` (its tail, if any, reaching ``tip``) in white with a black outline, then its
    words centred in it. ``below`` says the tail leaves from the bottom of the bubble (a top bubble)."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    a, b = (x1 - x0) / 2 - fitted.puff, (y1 - y0) / 2 - fitted.puff
    xs = [x0, x1] + ([tip[0]] if tip else [])
    ys = [y0, y1] + ([tip[1]] if tip else [])
    region = (math.floor(min(xs)) - 8, math.floor(min(ys)) - 8, math.ceil(max(xs)) + 8, math.ceil(max(ys)) + 8)
    size = ((region[2] - region[0]) * SUPERSAMPLE, (region[3] - region[1]) * SUPERSAMPLE)
    ink, paper = Image.new("L", size, 0), Image.new("L", size, 0)
    di, dp = ImageDraw.Draw(ink), ImageDraw.Draw(paper)

    def at(x: float, y: float) -> tuple[float, float]:
        return (x - region[0]) * SUPERSAMPLE, (y - region[1]) * SUPERSAMPLE

    def oval(draw: ImageDraw.ImageDraw, x: float, y: float, rx: float, ry: float) -> None:
        (ax, ay), (bx, by) = at(x - rx, y - ry), at(x + rx, y + ry)
        draw.ellipse([ax, ay, bx, by], fill=255)

    grow = STROKE
    if kind == "thought":
        steps = max(8, round(math.pi * (a + b) / (fitted.puff * 1.5)))
        centres = [(cx + a * math.cos(2 * math.pi * k / steps), cy + b * math.sin(2 * math.pi * k / steps))
                   for k in range(steps)]
        for draw, by in ((di, grow), (dp, 0)):
            oval(draw, cx, cy, a + by, b + by)
            for x, y in centres:
                oval(draw, x, y, fitted.puff + by, fitted.puff + by)
        if tip:  # a trail of puffs, smaller towards the tip
            sx = cx + (tip[0] - cx) * 0.4
            sy = _ellipse_y(cx, cy, a, b, sx, below)
            for t, r in ((0.3, 0.55), (0.65, 0.38), (1.0, 0.24)):
                x, y = sx + (tip[0] - sx) * t, sy + (tip[1] - sy) * t
                for draw, by in ((di, grow), (dp, 0)):
                    oval(draw, x, y, fitted.puff * r + by, fitted.puff * r + by)
    else:
        if tip:
            base = cx + {"left": -0.3, "center": 0.0, "right": 0.3}[tail] * a
            half = max(10.0, a * 0.14)
            corners = [(bx, cy + (_ellipse_y(cx, cy, a, b, bx, below) - cy) * 0.8) for bx in (base - half, base + half)]
            triangle = [at(*corners[0]), at(*tip), at(*corners[1])]
            di.polygon(triangle, fill=255)
            di.line(triangle + [triangle[0]], fill=255, width=2 * grow * SUPERSAMPLE, joint="curve")
        oval(di, cx, cy, a + grow, b + grow)
        oval(dp, cx, cy, a, b)
        if tip:
            dp.polygon(triangle, fill=255)
    area = (region[2] - region[0], region[3] - region[1])
    page.paste(INK, region, ink.resize(area, Image.Resampling.LANCZOS))
    page.paste(WHITE, region, paper.resize(area, Image.Resampling.LANCZOS))
    d = ImageDraw.Draw(page)
    lead = _lead(fitted.size)
    top = cy - len(fitted.lines) * lead / 2
    for k, line in enumerate(fitted.lines):
        d.text((cx, top + (k + 0.5) * lead), line, font=fitted.font, fill=INK, anchor="mm")


SIDE_ORDER = {"left": 0, "": 1, "right": 2}  # a position's side: "top-left" → left, "top" → ""


def _side(position: str) -> str:
    return position.partition("-")[2]


def _band(page: Image.Image, bubbles: list[tuple[int, Bubble]], inner: tuple[int, int, int, int],
          band: tuple[int, int], top: bool, wide: bool, cut: dict[int, Cut]) -> None:
    """Lay out and draw the ``bubbles`` (with their numbers in the panel) of one band of a panel: side by side in
    the order of their sides, from ``band[0]`` down (a top band) or up from ``band[1]`` (a bottom band), each tail
    pointing out of the band into the panel."""
    x0, _, x1, _ = inner
    gap = 18
    count = len(bubbles)
    slot = (x1 - x0 - gap * (count - 1)) / count
    max_w = slot if count > 1 else (x1 - x0) * (0.7 if wide else 0.86)
    room = band[1] - band[0]
    tail_len = max(22.0, min(64.0, room * 0.22))
    ordered = sorted(bubbles, key=lambda item: (SIDE_ORDER[_side(item[1].position)], item[0]))
    for place, (number, bubble) in enumerate(ordered):
        tail = tail_len if bubble.tail != "none" else 0.0
        fitted = fit_bubble(bubble.text, bubble.kind, max_w, room - tail)
        if fitted.cut:
            cut[number] = fitted.cut
        if not fitted.lines:
            continue
        w, h = fitted.width, fitted.height
        side = _side(bubble.position)
        if count > 1:
            left = x0 + place * (slot + gap)
            bx = left if place == 0 else left + slot - w if place == count - 1 else left + (slot - w) / 2
        else:
            bx = x0 if side == "left" else x1 - w if side == "right" else x0 + (x1 - x0 - w) / 2
        drop = min(room - tail - h, place * 30.0)  # a later bubble sits a little lower, as it is read later
        by = band[0] + drop if top else band[1] - h - drop
        tip = None
        if bubble.tail != "none":
            lean = {"left": -1, "center": 0, "right": 1}[bubble.tail]
            cx = bx + w / 2 + {"left": -0.3, "center": 0.0, "right": 0.3}[bubble.tail] * (w / 2 - fitted.puff)
            tx = min(max(cx + lean * w * 0.18, x0 + 4), x1 - 4)
            tip = (tx, by + h + tail if top else by - tail)
        _draw_bubble(page, fitted, (bx, by, bx + w, by + h), bubble.kind, bubble.tail, tip, below=top)


def _panel(page: Image.Image, photo: Image.Image, panel: Panel, box: tuple[int, int, int, int], index: int,
           cut: Cuts) -> None:
    """One panel: the photo cropped to fill ``box``, the caption, the bubbles and the border."""
    _photo(page, photo, box, mono=False)
    d = ImageDraw.Draw(page)
    x0, y0, x1, y1 = box
    inner = (x0 + BORDER + INSET, y0 + BORDER + INSET, x1 - BORDER - INSET, y1 - BORDER - INSET)
    top = inner[1]
    if panel.caption:
        bottom, caption_cut = _caption(d, panel.caption, inner[0], inner[1], inner[2] - inner[0])
        if caption_cut:
            cut[f"panels[{index}].caption"] = caption_cut
        top = bottom + 12
    placed = [(j, replace(b, position=b.position or DEFAULT_POSITIONS[min(j, len(DEFAULT_POSITIONS) - 1)]))
              for j, b in enumerate(panel.bubbles)]
    upper = [(j, b) for j, b in placed if b.position.startswith("top")]
    lower = [(j, b) for j, b in placed if b.position.startswith("bottom")]
    room = inner[3] - top
    bubble_cut: dict[int, Cut] = {}
    wide = x1 - x0 > W // 2
    if upper and lower:
        half = (room - 12) // 2
        _band(page, upper, inner, (top, top + half), True, wide, bubble_cut)
        _band(page, lower, inner, (inner[3] - half, inner[3]), False, wide, bubble_cut)
    elif upper:
        _band(page, upper, inner, (top, top + round(room * 0.62)), True, wide, bubble_cut)
    elif lower:
        _band(page, lower, inner, (inner[3] - round(room * 0.62), inner[3]), False, wide, bubble_cut)
    for j in sorted(bubble_cut):
        cut[f"panels[{index}].bubbles[{j}]"] = bubble_cut[j]
    d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=INK, width=BORDER)


def compose(photos: Sequence[Image.Image], comic: Comic | None = None) -> tuple[Image.Image, Cuts]:
    """The comic page: ``photos`` as its panels, in order, with the title, captions and bubbles of ``comic`` (the
    built-in comic cut to the number of photos when None); and the texts cut with "…" to fit, in reading order:
    ``title``, ``byline``, then each panel's ``panels[i].caption`` and ``panels[i].bubbles[j]``."""
    check_count(len(photos))
    comic = default_comic(len(photos)) if comic is None else comic
    check_panels(comic, len(photos))
    page = Image.new("RGB", (W, H), WHITE)
    cut: Cuts = {}
    top = _head(ImageDraw.Draw(page), comic, cut)
    for index, (photo, panel, box) in enumerate(zip(photos, comic.panels, panel_boxes(len(photos), top))):
        _panel(page, photo, panel, box, index, cut)
    return page, cut
