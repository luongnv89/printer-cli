"""The print guard: the one gate every command that prints goes through.

It answers two questions before anything is sent to the printer:

* **May this print?** In agent mode (``PRT_AGENT=1``) nothing prints without ``--yes``, even on a terminal.
  Page caps — ``max_pages_per_run`` and ``max_pages_per_day`` in the config file, and a default per-run cap
  in agent mode — are checked against the pages a job will use, and a job over a cap is refused whole.
* **What will it cost?** :func:`estimate` gives the page count, colour use and an ink estimate of rendered
  pages, for the preview JSON of booth, article and gazette.

Every refusal is a :class:`~prt.drivers.base.PrinterError` with its own ``kind`` so scripts, agents and the
MCP server can branch on it. Usage per day is kept in a small ledger in the cache folder. A page counts as
used once its submit has started, even when the submit then fails, because the printer may have taken it;
only pages never attempted are given back.

The guard keeps a cooperating agent from using up a cartridge by mistake. It is not a security boundary
against a hostile process, which can change its own environment and config.

Typical use::

    guard.check(cfg, pages, yes=yes)          # refuse early, before any prompt
    confirm()                                 # the command's own prompt, if it has one
    with guard.reserve(cfg, pages) as ticket:
        for page in pages:
            ticket.attempt(1)
            driver.submit(...)

or ``with guard.authorize(cfg, pages, yes=yes) as ticket:`` when there is no prompt in between.
"""

from __future__ import annotations

import json
import os
import re
import time
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prt.config import cache_dir
from prt.drivers.base import PrinterError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: the ledger is updated without a lock
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from PIL import Image

    from prt.config import Config

AGENT_ENV = "PRT_AGENT"
DEFAULT_AGENT_MAX_PAGES_PER_RUN = 10  # per-run cap in agent mode when the config sets none
LEDGER_FILE = "pages.json"

# Ink model for the estimate: a standard cartridge prints about this many pages at 5% coverage of its colour
# (the ISO/IEC 24711 way of rating cartridges). Good enough to tell "a few %" from "half a cartridge".
CARTRIDGE_PAGES = 300
RATED_COVERAGE = 0.05
INKS = ("cyan", "magenta", "yellow", "black")
CHROMA_THRESHOLD = 16  # 0-255: a pixel whose channels differ by more than this is coloured
COLOUR_SHARE = 0.001  # a page is colour when more than this share of its pixels is coloured
ESTIMATE_SIZE = 256  # pages are averaged down to about this many pixels on their long side

# --- Agent mode -------------------------------------------------------------------------------------------


def agent_mode() -> bool:
    """True when ``PRT_AGENT`` is set to 1, true, yes or on (any case)."""
    return os.environ.get(AGENT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def require_consent(yes: bool) -> None:
    """In agent mode, refuse unless ``--yes`` was given — whether or not a terminal is attached."""
    if agent_mode() and not yes:
        raise PrinterError(f"agent mode ({AGENT_ENV}=1): this command prints or uses ink only with --yes",
                           hint="check the job first (--preview prints nothing), then re-run with --yes",
                           exit_code=2, kind="agent_confirmation_required")


# --- Page caps --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Caps:
    run: int | None = None
    day: int | None = None

    @property
    def active(self) -> bool:
        return self.run is not None or self.day is not None


def caps(cfg: Config | None) -> Caps:
    """The caps in force: the config's, plus the default per-run cap in agent mode."""
    run = cfg.max_pages_per_run if cfg else None
    if run is None and agent_mode():
        run = DEFAULT_AGENT_MAX_PAGES_PER_RUN
    return Caps(run=run, day=cfg.max_pages_per_day if cfg else None)


def _where(cfg: Config | None) -> str:
    return f" in {cfg.path}" if cfg else " in the config file"


def _check_caps(cfg: Config | None, c: Caps, pages: int | None, used_today: int) -> None:
    if not c.active:
        return
    if pages is None:
        raise PrinterError("cannot tell how many pages this would print, and a page cap is in force",
                           hint="print images or a PDF, whose pages can be counted, or convert the file first",
                           kind="page_count_unknown")
    if c.run is not None and pages > c.run:
        raise PrinterError(f"{_pages(pages)} is over the limit of {c.run} per run — nothing was sent",
                           hint=f"print fewer pages at a time, or raise max_pages_per_run{_where(cfg)}",
                           kind="page_cap_exceeded")
    if c.day is not None and used_today + pages > c.day:
        raise PrinterError(f"{_pages(pages)} would make {used_today + pages} today, over the limit of {c.day} "
                           "per day — nothing was sent",
                           hint=f"{max(0, c.day - used_today)} left today; wait until tomorrow, or raise "
                                f"max_pages_per_day{_where(cfg)}",
                           kind="daily_page_cap_exceeded")


def _pages(n: int) -> str:
    return f"{n} page{'s' if n != 1 else ''}"


def check(cfg: Config | None, pages: int | None, *, yes: bool) -> None:
    """Refuse a print early: agent-mode consent first, then the caps. Sends and records nothing.

    ``pages`` is how many sheets the job will use, or None when that cannot be told.
    """
    require_consent(yes)
    c = caps(cfg)
    _check_caps(cfg, c, pages, used_today() if c.day is not None else 0)


# --- Daily ledger -----------------------------------------------------------------------------------------


def ledger_path() -> Path:
    return cache_dir() / LEDGER_FILE


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _used(text: str, day: str) -> int:
    try:
        data = json.loads(text)
    except ValueError:
        return 0
    pages = data.get("pages") if isinstance(data, dict) and data.get("date") == day else 0
    return pages if isinstance(pages, int) and not isinstance(pages, bool) and pages > 0 else 0


def used_today(path: Path | None = None) -> int:
    """Pages printed (or attempted) today, from the ledger; 0 when it is missing or unreadable."""
    try:
        return _used((path or ledger_path()).read_text(), _today())
    except OSError:
        return 0


def _update(path: Path, change) -> int:
    """Apply ``change(used) -> new`` to today's count under an exclusive lock; ``change`` may raise to refuse."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX)
        day = _today()
        new = change(_used(f.read(), day))
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"date": day, "pages": new}))
        f.flush()
        return new


class Ticket:
    """What a reservation hands the command: call ``attempt(n)`` right before each submit of n pages."""

    def __init__(self, reserved: int):
        self.reserved = reserved
        self.attempted = 0

    def attempt(self, pages: int = 1) -> None:
        self.attempted += pages


@contextmanager
def reserve(cfg: Config | None, pages: int | None) -> Iterator[Ticket]:
    """Book ``pages`` in today's ledger (checking the caps again, atomically) for the duration of the block.

    When the block ends, normally or by an exception, the pages it never attempted are given back. With a
    daily cap in force, a ledger that cannot be written refuses the print; without one it is best effort.
    """
    c = caps(cfg)
    _check_caps(cfg, c, pages, 0)
    planned = pages or 0
    path = ledger_path()

    def book(used: int) -> int:
        _check_caps(cfg, Caps(day=c.day), planned, used)
        return used + planned

    day, booked = _today(), True
    try:
        _update(path, book)
    except OSError as exc:
        if c.day is not None:
            raise PrinterError(f"cannot record printed pages in {path}: {exc.strerror or exc}",
                               hint="max_pages_per_day needs a writable cache folder; check its permissions",
                               kind="page_ledger_unavailable") from exc
        booked = False
    ticket = Ticket(planned)
    try:
        yield ticket
    finally:
        unsent = planned - min(ticket.attempted, planned)
        if booked and unsent and _today() == day:
            try:
                _update(path, lambda used: max(0, used - unsent))
            except OSError:
                pass  # giving pages back is a courtesy; the count stays on the safe side


@contextmanager
def authorize(cfg: Config | None, pages: int | None, *, yes: bool) -> Iterator[Ticket]:
    """``check`` and ``reserve`` in one, for callers with no prompt of their own (TUI, MCP server)."""
    check(cfg, pages, yes=yes)
    with reserve(cfg, pages) as ticket:
        yield ticket


# --- Counting the pages of a file -------------------------------------------------------------------------

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_INFLATED_BYTES = 64 * 1024 * 1024

_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z0-9])")
_OBJ = re.compile(rb"\d+\s+\d+\s+obj\b(.*?)endobj", re.S)
_OBJSTM = re.compile(rb"/Type\s*/ObjStm")
_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)
_COUNT = re.compile(rb"/Count\s+(\d+)")
_FIRST = re.compile(rb"/First\s+(\d+)")
_TYPE = re.compile(rb"/Type\s*/([A-Za-z]+)")


def count_file_pages(path: Path) -> int | None:
    """Sheets one copy of ``path`` prints, as ``prt print`` sends it, or None when that cannot be told.

    Images print on one page (only the first frame is sent). PDF pages are counted from the file. Other files
    (text, PWG raster, office documents) are None.
    """
    ext = path.suffix.lower()
    if ext in IMAGE_SUFFIXES:
        return 1
    if ext == ".pdf":
        try:
            if path.stat().st_size > MAX_PDF_BYTES:
                return None
            return count_pdf_pages(path.read_bytes())
        except OSError:
            return None
    return None


def _decode_names(data: bytes) -> bytes:
    return _NAME_ESCAPE.sub(lambda m: bytes([int(m[1], 16)]), data)


def _inflate(raw: bytes) -> bytes | None:
    d = zlib.decompressobj()
    try:
        out = d.decompress(raw, MAX_INFLATED_BYTES)
    except zlib.error:
        return None
    return None if d.unconsumed_tail else out


def _objstm_objects(body: bytes) -> list[bytes] | None:
    """The objects packed in one object stream, or None when it cannot be read."""
    head, _, rest = body.partition(b"stream")
    m = _STREAM.search(b"stream" + rest)
    first = _FIRST.search(head)
    data = _inflate(m.group(1)) if m else None
    if data is None or first is None:
        return None
    start = int(first.group(1))
    numbers = data[:start].split()
    offsets = sorted(int(n) for n in numbers[1::2] if n.isdigit())
    ends = offsets[1:] + [len(data) - start]
    return [data[start + a:start + b] for a, b in zip(offsets, ends)]


def count_pdf_pages(data: bytes) -> int | None:
    """Pages of a PDF, counted so that it errs high rather than low; None when it cannot be told.

    The objects are read from the file and from its compressed object streams, with ``#xx`` name escapes
    decoded. The result is the largest of three counts: page objects (``/Type /Page``), page-like leaves (a
    ``/Parent`` with ``/MediaBox``, ``/Contents`` or ``/Resources``, no ``/Kids`` and no ``/Subtype``) and the
    biggest ``/Count`` of a page-tree node. An object stream that cannot be inflated makes the count None.
    """
    objects: list[bytes] = []
    for m in _OBJ.finditer(data):
        body = m.group(1)
        head = _decode_names(body.split(b"stream", 1)[0])
        if _OBJSTM.search(head):
            packed = _objstm_objects(body)
            if packed is None:
                return None
            objects += [_decode_names(o) for o in packed]
        else:
            objects.append(head)
    typed = leaves = biggest = 0
    for obj in objects:
        types = set(_TYPE.findall(obj))
        if _PAGE.search(obj):
            typed += 1
        if (b"/Parent" in obj and b"/Kids" not in obj and b"/Subtype" not in obj
                and types <= {b"Page"} and any(k in obj for k in (b"/MediaBox", b"/Contents", b"/Resources"))):
            leaves += 1
        if b"/Kids" in obj:
            biggest = max([biggest, *(int(n) for n in _COUNT.findall(obj))])
    return max(typed, leaves, biggest) or None


def job_pages(files: Iterable[Path], copies: int = 1) -> int | None:
    """Sheets a ``prt print`` of these files uses; None when one of them cannot be counted.

    A missing file counts as 0: printing stops on it before it is sent.
    """
    total = 0
    for f in files:
        n = count_file_pages(f) if f.is_file() else 0
        if n is None:
            return None
        total += n * max(1, copies)
    return total


# --- Cost estimate ------------------------------------------------------------------------------------------


def _coverage(page: Image.Image) -> tuple[dict[str, float], bool]:
    """Ink coverage (0-1) of each colour of one page, and whether the page uses colour at all."""
    from PIL import ImageChops, ImageStat
    rgb = page.convert("RGB")
    factor = max(1, max(rgb.size) // ESTIMATE_SIZE)
    if factor > 1:
        rgb = rgb.reduce(factor)
    r, g, b = rgb.split()
    high = ImageChops.lighter(ImageChops.lighter(r, g), b)
    low = ImageChops.darker(ImageChops.darker(r, g), b)
    # Grey component replacement: black prints the darkness all three share, colour only the difference.
    bands = [ImageChops.subtract(high, r), ImageChops.subtract(high, g), ImageChops.subtract(high, b),
             ImageChops.invert(high)]
    coverage = {ink: ImageStat.Stat(band).mean[0] / 255 for ink, band in zip(INKS, bands)}
    chroma = ImageChops.subtract(high, low).histogram()
    colour = sum(chroma[CHROMA_THRESHOLD + 1:]) > COLOUR_SHARE * rgb.width * rgb.height
    return coverage, colour


def estimate(pages: Iterable[Image.Image]) -> dict[str, Any]:
    """What printing these rendered pages costs, roughly.

    ``ink`` gives, for each colour, the estimated share of one standard cartridge the job uses, in percent;
    ``ink_percent`` is the highest of them (the cartridge that runs out first). ``summary`` reads like
    "~3 pages, colour, est. ink 4%".
    """
    used = dict.fromkeys(INKS, 0.0)
    count, colour = 0, False
    for page in pages:
        coverage, coloured = _coverage(page)
        count += 1
        colour = colour or coloured
        for ink, share in coverage.items():
            used[ink] += share / RATED_COVERAGE / CARTRIDGE_PAGES * 100
    if not colour:  # a black-and-white job: the printer mixes no colour in
        used.update(cyan=0.0, magenta=0.0, yellow=0.0)
    ink = {k: round(v, 1) for k, v in used.items()}
    top = max(used.values()) if count else 0.0
    shown = f"{top:.0f}%" if top >= 0.5 else "<1%"
    summary = f"~{_pages(count)}, {'colour' if colour else 'black and white'}, est. ink {shown}"
    return {"pages": count, "colour": colour, "ink": ink, "ink_percent": round(top, 1), "summary": summary}
