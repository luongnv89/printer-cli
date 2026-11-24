"""Disc label: a photo and a track list on a printable 12 cm CD or DVD, printed through the disc tray.

The label is drawn on :data:`prt.paper.DISC`, the 120 mm square around the disc, at 300 dpi (1417 x 1417 px). The
photo fills the printable ring, :data:`OUTER_D_MM` across with a :data:`INNER_D_MM` hole round the hub (the
printable area most inkjet printers default to on a 12 cm disc). The title and an optional subtitle are written above
the hub; the tracks run in two columns down either side of it, the first half on the left; an optional footer line
sits at the foot. Every line is fitted to the width the ring has at its height, so no text reaches the hub or the edge.
The text is set in the bundled Caveat handwriting font, off-white with a dark outline so it reads on any photo;
letters it lacks print without their accents, and text it cannot print at all, such as emoji or Chinese, is refused
before anything is drawn.

The last step (:func:`clip`) whitens everything outside the ring, the corners of the square and the hub, so the printer
puts no ink on the tray or on the clear centre of the disc, and the ink estimate counts the ring alone. The JPEG the
printer is sent can ring by up to about 1.5 mm across the ring's edges (faint, near-white). That stays on the disc,
clear of the tray, and well clear of the centre hole.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from prt import fonts
from prt.booth import STDIN, read_stdin
from prt.drivers.base import PrinterError
from prt.layout import Cut, Cuts, _clean
from prt.paper import DISC
from prt.vintage import _char_in, _fit_face

PAPER = DISC
W, H = PAPER.size  # 1417 x 1417
C = W // 2  # the centre of the disc, in both directions
KIND = "disc label"

OUTER_D_MM = 116  # the printable ring's outer diameter: 2 mm inside the edge of a 120 mm disc
INNER_D_MM = 43  # the printable ring's inner diameter: the hub and the clear ring round it stay white
R_OUT = PAPER.mm(OUTER_D_MM) // 2  # 685 px
R_IN = PAPER.mm(INNER_D_MM) // 2  # 254 px

# --- the label text ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Disc:
    title: str
    subtitle: str = ""
    tracks: tuple[str, ...] = ()
    footer: str = ""


FIELDS = tuple(f.name for f in fields(Disc))
REQUIRED = ("title",)
# Upper bounds that keep the text on the ring; text that still overflows its line ends in "…", and compose() names
# the fields that were cut, with how much of each was printed, as prt booth does.
LIMITS = {"title": 60, "subtitle": 80, "footer": 60}
MAX_TRACKS = 24
MAX_TRACK = 80  # characters in one track
MAX_DISC_FILE = 64 * 1024
EXPECTED = 'expected {"title": "...", "tracks": ["...", "..."]}'

DEFAULT_DISC = Disc(
    title="Road Trip Mix",
    subtitle="Summer 2026",
    tracks=("1. Open Road", "2. Windows Down", "3. Static on the Radio", "4. Gas Station Coffee",
            "5. Sunset Exit", "6. Coast Highway", "7. Back Seat Singalong", "8. Last Mile Home"),
    footer="For Sam, from Jo",
)


def _printable_check(name: str, value: str, exit_code: int) -> None:
    face = fonts.face("hand", 40)
    unprintable = sorted({c for c in value if c != "?" and _char_in(face, c) == "?"})
    if unprintable:
        shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
        raise PrinterError(f"{name} has characters the handwriting font cannot print: {shown}",
                           hint="write it in Latin or Cyrillic letters (no emoji)", exit_code=exit_code)


def check_disc(disc: Disc, exit_code: int) -> Disc:
    """Normalise whitespace, drop empty tracks and enforce the required title, the length limits, the number of
    tracks and printable characters."""
    clean: dict[str, Any] = {}
    for name in LIMITS:
        value = _clean(getattr(disc, name), keep_newlines=False)
        if name in REQUIRED and not value:
            raise PrinterError(f"the disc label has an empty {name}", hint=f"give the {name} some text",
                               exit_code=exit_code)
        if len(value) > LIMITS[name]:
            raise PrinterError(f"{name} is too long ({len(value)} characters, max {LIMITS[name]})",
                               hint=f"shorten the {name} so it fits on the disc", exit_code=exit_code)
        _printable_check(name, value, exit_code)
        clean[name] = value
    tracks = [track for track in (_clean(t, keep_newlines=False) for t in disc.tracks) if track]
    if len(tracks) > MAX_TRACKS:
        raise PrinterError(f"the disc label has {len(tracks)} tracks, more than {MAX_TRACKS}",
                           hint=f"list at most {MAX_TRACKS} tracks", exit_code=exit_code)
    for number, track in enumerate(tracks, 1):
        if len(track) > MAX_TRACK:
            raise PrinterError(f"track {number} is too long ({len(track)} characters, max {MAX_TRACK})",
                               hint="shorten the track so it fits on the disc", exit_code=exit_code)
        _printable_check(f"track {number}", track, exit_code)
    return Disc(tracks=tuple(tracks), **clean)


def load_disc(path: Path) -> Disc:
    """Read a disc label JSON file: {"title", "subtitle"?, "tracks"?, "footer"?}; ``-`` reads stdin."""
    if str(path) == STDIN:
        name = kind = "disc label on stdin"
        content = read_stdin(MAX_DISC_FILE)
        too_big = len(content) > MAX_DISC_FILE
    else:
        if not path.is_file():
            raise PrinterError(f"disc file not found: {path}", hint="check the path", exit_code=3)
        name, kind = f"disc file {path.name}", "disc file"
        too_big = path.stat().st_size > MAX_DISC_FILE
        content = b"" if too_big else path.read_bytes()
    if too_big:
        raise PrinterError(f"{name} is larger than {MAX_DISC_FILE // 1024} KiB",
                           hint="a disc label holds a title and a track list", exit_code=3)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8 JSON", exit_code=3) from exc
    return parse_disc(text, name, kind)


def parse_disc(text: str, name: str = "disc label", kind: str = "disc label") -> Disc:
    """A disc label from its JSON text, checked as a disc file is; ``tracks`` is a list of texts, or one text with a
    newline between tracks. ``name`` and ``kind`` say where it came from in error messages (e.g. ``disc file
    disc.json`` and ``disc file``)."""
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

    def is_tracks(value: Any) -> bool:
        return isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))
    wrong = sorted(field for field, value in data.items()
                   if not (is_tracks(value) if field == "tracks" else isinstance(value, str)))
    if wrong:
        raise PrinterError(f"{kind} fields must be text: {', '.join(wrong)}",
                           hint="tracks is a list of texts (or one text, a newline between tracks)", exit_code=3)
    tracks = data.pop("tracks", ())
    tracks = tuple(tracks.split("\n") if isinstance(tracks, str) else tracks)
    return check_disc(Disc(tracks=tracks, **data), exit_code=3)


def with_overrides(base: Disc, title: str | None = None, subtitle: str | None = None, footer: str | None = None,
                   tracks: list[str] | None = None) -> Disc:
    """Apply command-line text over a disc label; ``None`` keeps the base value, and ``tracks`` replaces the whole
    list."""
    changes: dict[str, Any] = {k: v for k, v in (("title", title), ("subtitle", subtitle), ("footer", footer))
                               if v is not None}
    if tracks is not None:
        changes["tracks"] = tuple(tracks)
    return check_disc(replace(base, **changes), exit_code=2)


# --- the layout --------------------------------------------------------------------------------

EDGE = 30  # text keeps this far inside the outer edge of the ring (2.5 mm)
HUB_PAD = 22  # and this far outside the hub
R_TEXT = R_OUT - EDGE
R_HUB = R_IN + HUB_PAD
SIDE = 12  # either side of a line, inside the width the ring has at its height
GUTTER = 36  # between the two track columns, below the hub
TEXT = (252, 251, 246)  # the handwriting: off-white, so it never reads as the bare white round the ring
OUTLINE = (24, 22, 30)
TITLE_SIZE, TITLE_MIN = 120, 76  # on one line; a title too long for it goes on two, from 72 down to 48
TITLE_TWO, TITLE_TWO_MIN = 72, 48
SUBTITLE_SIZE, SUBTITLE_MIN = 56, 36
TRACK_SIZE, TRACK_MIN = 44, 26
FOOTER_SIZE, FOOTER_MIN = 38, 26
FOOTER_RISE = 60  # the footer's line ends this far above the foot of the text area, where the ring is wider


def _half(r: float, dy: float) -> float:
    """Half the width of a circle of radius ``r`` at ``dy`` from its centre."""
    return math.sqrt(r * r - dy * dy) if abs(dy) < r else 0.0


def _span(top: float, bottom: float) -> tuple[float, float]:
    """How far from the centre line text may go across the whole band ``top``..``bottom``: (outer, inner).

    ``outer`` is the text circle's half-width at the band's narrower edge, ``inner`` the padded hub's half-width at
    the point of the band nearest the centre (0 when the band misses the hub)."""
    outer = min(_half(R_TEXT, top - C), _half(R_TEXT, bottom - C))
    nearest = 0.0 if top <= C <= bottom else min(abs(top - C), abs(bottom - C))
    return outer, _half(R_HUB, nearest)


def _band(size: int) -> tuple[float, float]:
    """How far the glyphs of a Caveat line at ``size`` reach above and below its baseline."""
    return 0.74 * size, 0.26 * size


def _stroke(size: int) -> int:
    return max(2, size // 14)


def _across(baseline: float, size: int) -> float:
    """The width a centred line at ``baseline`` may take across the ring above or below the hub."""
    up, down = _band(size)
    outer, _ = _span(baseline - up, baseline + down)
    return max(0.0, 2 * outer - 2 * (SIDE + _stroke(size)))


def _draw(d: ImageDraw.ImageDraw, x: float, baseline: float, text: str, font, size: int) -> None:
    d.text((x, baseline), text, font=font, fill=TEXT, anchor="ms", stroke_width=_stroke(size), stroke_fill=OUTLINE)


def _fit_upward(text: str, role: str, size: int, min_size: int, max_lines: int, bottom: float, lead: float):
    """The largest size (``size`` down to ``min_size``) at which ``text`` wraps into ``max_lines`` centred lines that
    each fit the ring, the glyphs of the last line ending at ``bottom``; cut with "…" at the smallest. Returns (font,
    size, baselines, lines, cut): ``baselines`` of the lines printed, from the top, and the rest as
    ``vintage._fit_face`` gives them."""
    for s in range(size, min_size - 1, -2):
        step = round(s * lead)
        last = bottom - _band(s)[1]
        baselines = [last - step * (max_lines - 1 - k) for k in range(max_lines)]
        width = min(_across(b, s) for b in baselines)
        font, s, lines, cut = _fit_face(text, role, s, s, round(width), max_lines)
        if cut is None:
            break
    return font, s, baselines[max_lines - len(lines):], lines, cut


def _head(d: ImageDraw.ImageDraw, disc: Disc, cut: Cuts) -> None:
    """The title (on one line, else on two) over the subtitle, centred above the hub."""
    bottom = C - R_HUB
    subtitle = None
    if disc.subtitle:
        subtitle = _fit_upward(disc.subtitle, "hand", SUBTITLE_SIZE, SUBTITLE_MIN, 1, bottom, 1.1)
        if subtitle[2]:
            bottom = subtitle[2][0] - _band(subtitle[1])[0] - 10
    title = _fit_upward(disc.title, "hand-bold", TITLE_SIZE, TITLE_MIN, 1, bottom, 1.05)
    if title[4] is not None:
        title = _fit_upward(disc.title, "hand-bold", TITLE_TWO, TITLE_TWO_MIN, 2, bottom, 1.05)
    for name, fitted in (("title", title), ("subtitle", subtitle)):
        if fitted is None:
            continue
        font, size, baselines, lines, line_cut = fitted
        if line_cut:
            cut[name] = line_cut
        for baseline, line in zip(baselines, lines):
            _draw(d, C, baseline, line, font, size)


def _footer(d: ImageDraw.ImageDraw, footer: str, cut: Cuts) -> float:
    """The footer line, centred at the foot of the ring; returns where the tracks above it must end."""
    bottom = C + R_TEXT - FOOTER_RISE
    if not footer:
        return bottom
    font, size, baselines, lines, foot_cut = _fit_upward(footer, "hand", FOOTER_SIZE, FOOTER_MIN, 1, bottom, 1.1)
    if foot_cut:
        cut["footer"] = foot_cut
    if not lines:  # nothing to print, e.g. only combining marks: the tracks may run down to the foot
        return bottom
    _draw(d, C, baselines[0], lines[0], font, size)
    return baselines[0] - _band(size)[0] - 14


@dataclass(frozen=True)
class Slot:
    """Where one track is written: centred on ``x`` at ``baseline``, at most ``width`` wide."""
    x: float
    baseline: float
    width: float


def _slots(n: int, top: float, bottom: float, size: int) -> list[Slot | None]:
    """The line of each of ``n`` tracks at ``size``, in two columns either side of the centre line, the first half
    (rounded up) on the left; the rows are centred on the hub when there is room. A track with no row left between
    ``top`` and ``bottom`` gets None. A lone track is centred under the hub instead."""
    lead = round(size * 1.1)
    up, down = _band(size)
    pad = SIDE + _stroke(size)
    if n == 1:
        baseline = C + R_HUB + up
        return [Slot(C, baseline, _across(baseline, size)) if baseline + down <= bottom else None]
    rows = math.ceil(n / 2)
    shown = max(0, min(rows, int((bottom - top) // lead)))
    start = min(max(top, C - shown * lead / 2), bottom - shown * lead)
    out: list[Slot | None] = []
    for i in range(n):
        column, row = (0, i) if i < rows else (1, i - rows)
        if row >= shown:
            out.append(None)
            continue
        baseline = start + row * lead + up
        outer, inner = _span(baseline - up, baseline + down)
        inner = max(inner, GUTTER / 2)
        middle = (outer + inner) / 2
        out.append(Slot(C - middle if column == 0 else C + middle, baseline, max(0.0, outer - inner - 2 * pad)))
    return out


def _tracks(d: ImageDraw.ImageDraw, tracks: tuple[str, ...], top: float, bottom: float, cut: Cuts) -> None:
    """The tracks either side of the hub, as large as lets every track fit its line (see ``_slots``). At the smallest
    size a track too long for its line ends in "…" and a track with no line left is left out; ``tracks`` is then
    named in ``cut``, counted in the characters of the tracks joined by newlines."""
    if not tracks:
        return
    for size in range(TRACK_SIZE, TRACK_MIN - 1, -2):
        slots = _slots(len(tracks), top, bottom, size)
        fitted = [None if slot is None else _fit_face(track, "hand", size, size, round(slot.width), 1)
                  for track, slot in zip(tracks, slots)]
        if all(f is not None and f[3] is None for f in fitted):
            break
    given = len("\n".join(tracks))
    done = 0  # characters of the tracks before the one being written, with the newline after each
    for track, slot, f in zip(tracks, slots, fitted):
        if slot is None or f is None:
            cut.setdefault("tracks", Cut(max(0, done - 1), given))
        else:
            font, _, lines, line_cut = f
            if lines:
                _draw(d, slot.x, slot.baseline, lines[0], font, size)
            if line_cut:
                cut.setdefault("tracks", Cut(done + line_cut.max_fit, given))
        done += len(track) + 1


def ring() -> Image.Image:
    """The printable ring as a mask: 255 from :data:`R_IN` out to :data:`R_OUT` round the centre, 0 elsewhere."""
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((C - R_OUT, C - R_OUT, C + R_OUT, C + R_OUT), fill=255)
    d.ellipse((C - R_IN, C - R_IN, C + R_IN, C + R_IN), fill=0)
    return mask


def clip(page: Image.Image) -> Image.Image:
    """``page`` with everything outside the printable ring white: the corners, beyond the outer edge and the hub."""
    return Image.composite(page.convert("RGB"), Image.new("RGB", (W, H), (255, 255, 255)), ring())


def compose(photo: Image.Image, disc: Disc | None = None) -> tuple[Image.Image, Cuts]:
    """The label: ``photo`` cropped to fill the ring, the title and subtitle above the hub, the tracks either side
    of it and the footer at the foot, clipped to the ring; and the fields cut with "…" to fit, in the order the label
    is read: title, subtitle, tracks, footer. Uses the default label when none is given."""
    disc = DEFAULT_DISC if disc is None else disc
    page = Image.new("RGB", (W, H), (255, 255, 255))
    side = 2 * R_OUT + 1
    fitted = ImageOps.fit(photo.convert("RGB"), (side, side), method=Image.Resampling.LANCZOS)
    page.paste(fitted, (C - R_OUT, C - R_OUT))
    d = ImageDraw.Draw(page)
    head: Cuts = {}
    _head(d, disc, head)
    foot: Cuts = {}
    bottom = _footer(d, disc.footer, foot)
    tracks: Cuts = {}
    _tracks(d, disc.tracks, C - R_HUB, bottom, tracks)
    return clip(page), {**head, **tracks, **foot}
