"""btop-inspired terminal styling, in prt's "mono + real ink" palette.

Layout vocabulary from btop (rounded boxes with the title notched into the border, cell
meters, status symbols); colour from prt's palette:
  * chrome is monochrome — terminal default text, bold titles, gray (#6B7280) borders,
    bright green (#22C55E) as the single accent;
  * ink meters are drawn in the cartridge's real colour (the only colour on screen);
  * status uses green / amber (#F59E0B) / red (#EF4444) for the *symbol*, and the word
    beside it stays in the default text colour, so colour never carries meaning alone.

Every render function takes an explicit Mode, so tests can render each path without a TTY.
"""

from __future__ import annotations

import io
import math
import os
import re
import sys
from dataclasses import dataclass
from typing import TextIO

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

# --- Palette roles (command code names roles, never escape codes) -------------
ROLES = {
    "title": "bold",
    "accent": "bold #22C55E",
    "dim": "dim",
    "border": "#6B7280",
    "ok": "#22C55E",
    "warn": "#F59E0B",
    "err": "bold #EF4444",
    "info": "#6B7280",
}

TIERS = {"none": None, "16": "standard", "256": "256", "truecolor": "truecolor"}


@dataclass(frozen=True)
class Mode:
    styled: bool = True
    tier: str = "truecolor"  # none | 16 | 256 | truecolor
    unicode: bool = True
    width: int = 80

    @property
    def color(self) -> bool:
        return self.styled and self.tier != "none"


PLAIN = Mode(styled=False, tier="none", unicode=False, width=80)

UNI = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│", "nl": "┐", "nr": "┌",
       "fl": "┘", "fr": "└", "lt": "├", "rt": "┤", "cell": "■", "ok": "●", "warn": "▲",
       "err": "✗", "info": "◆", "off": "○", "ell": "…", "dot": "·", "check": "✓"}
ASC = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|", "nl": "[", "nr": "]",
       "fl": "[", "fr": "]", "lt": "+", "rt": "+", "cell": "#", "ok": "+", "warn": "!",
       "err": "x", "info": "i", "off": "o", "ell": "...", "dot": "-", "check": "v"}


def glyphs(mode: Mode) -> dict[str, str]:
    return UNI if mode.unicode else ASC


# --- Section 2: the three decisions per stream ---------------------------------

def _truthy(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0")


def decide(stream: TextIO, *, machine: bool = False, no_color: bool = False,
           is_stdout: bool = True) -> Mode:
    """Styled-or-plain, colour tier and glyph set for one output stream."""
    width = _width(stream)
    term = os.environ.get("TERM", "")
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        tty = False
    if (is_stdout and machine) or term == "dumb" or (not tty and not _truthy("FORCE_COLOR")):
        return Mode(styled=False, tier="none", unicode=False, width=width)
    if no_color or os.environ.get("NO_COLOR", "") != "":
        tier = "none"
    elif os.environ.get("COLORTERM", "") in ("truecolor", "24bit"):
        tier = "truecolor"
    elif "256color" in term:
        tier = "256"
    else:
        tier = "16"
    enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    return Mode(styled=True, tier=tier, unicode=enc == "utf8", width=width)


def _width(stream: TextIO) -> int:
    if os.environ.get("COLUMNS", "").isdigit():
        return int(os.environ["COLUMNS"])
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (OSError, AttributeError, ValueError, io.UnsupportedOperation):
        return 80


# --- Text safety ---------------------------------------------------------------

def escape(value: object, plain: bool = False) -> str:
    """Make printer-supplied text safe: control characters become visible escapes."""
    out = []
    for ch in str(value):
        o = ord(ch)
        if ch == "\\" and plain:
            out.append("\\\\")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif o < 0x20 or o == 0x7F or 0x80 <= o <= 0x9F:
            out.append(f"\\x{o:02x}")
        else:
            out.append(ch)
    return "".join(out)


def plain_record(*fields: object) -> str:
    return "\t".join(escape("" if f is None else f, plain=True) for f in fields)


def seg(text: str, role: str | None = None) -> Text:
    """Trusted (CLI-written) text with a style *span* — appending to it never inherits the style."""
    out = Text()
    out.append(text, style=ROLES.get(role, role) if role else "")
    return out


def t(value: object, role: str | None = None) -> Text:
    """User/printer-supplied text as a rich Text (never parsed as markup)."""
    return seg(escape(value), role)


# --- Rendering helpers ---------------------------------------------------------

def render(mode: Mode, *items: Text | str) -> str:
    """Render rich Text objects to a string with exactly the escapes this mode allows."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=mode.styled, color_system=TIERS[mode.tier] if mode.styled else None,
                      highlight=False, emoji=False, markup=False, width=max(mode.width, 20),
                      no_color=False, legacy_windows=False)
    for item in items:
        console.print(item, end="", soft_wrap=True, crop=False)
    out = buf.getvalue()
    return out if mode.unicode else to_ascii(out)


ASCII_MAP = str.maketrans({"×": "x", "—": "-", "–": "-", "…": "...", "·": "-", "✓": "v", "✗": "x",
                           "●": "+", "▲": "!", "◆": "i", "○": "o", "■": "#", "→": "->", "°": "o"})


def to_ascii(s: str) -> str:
    """ASCII glyph set: transliterate our own symbols; anything else non-ASCII becomes '?'."""
    s = s.translate(ASCII_MAP)
    return "".join(c if ord(c) < 0x80 else "?" for c in s)


def _round(x: float) -> int:
    return math.floor(x + 0.5)


def fit(text: Text, width: int, mode: Mode) -> Text:
    """Truncate with an ellipsis, then pad, measuring terminal cells."""
    text = text.copy()
    if cell_len(text.plain) > width:
        ell = glyphs(mode)["ell"]
        text.truncate(max(0, width - cell_len(ell)))
        text.append(ell)
    text.pad_right(max(0, width - cell_len(text.plain)))
    return text


# --- Components (section 4) -------------------------------------------------------

_HEX = re.compile(r"#?([0-9A-Fa-f]{6})")


def ink_colors(value: str) -> list[str]:
    """Parse an IPP marker-colors value into '#RRGGBB' strings.

    A single cartridge is '#RRGGBB'; a multi-colour one concatenates them
    ('#00FFFF#FF00FF#FFFF00' for an HP tri-colour); 'none' or junk gives []."""
    return [f"#{m.upper()}" for m in _HEX.findall(value or "")]


def is_dark(hex_color: str) -> bool:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.2


def ink_style(color: str) -> str:
    """Real cartridge colour (the first, for a multi-colour cartridge); near-black inks use the
    terminal's default foreground so they stay visible on dark terminals."""
    colors = ink_colors(color)
    if not colors or is_dark(colors[0]):
        return "bold"
    return colors[0]


def meter(value: int | None, width: int, mode: Mode, fill_style: str = "bold") -> Text:
    """btop meter: cell i is filled when value >= round(i*100/width)."""
    g = glyphs(mode)
    out = Text()
    for i in range(1, width + 1):
        filled = value is not None and value >= _round(i * 100 / width)
        if filled:
            out.append(g["cell"], style=fill_style if mode.color else "")
        elif not mode.unicode:
            out.append(".")
        elif mode.color:
            out.append(g["cell"], style="dim")
        else:
            out.append("·")
    out.append(f" {value:>3}%" if value is not None else "   ?%")
    return out


SEVERITY_ROLE = {"ok": "ok", "info": "info", "warning": "warn", "warn": "warn", "error": "err", "err": "err",
                 "offline": "dim"}
SEVERITY_GLYPH = {"ok": "ok", "info": "info", "warning": "warn", "warn": "warn", "error": "err",
                  "err": "err", "offline": "off"}


def status(level: str, word: str, mode: Mode) -> Text:
    g = glyphs(mode)
    out = Text()
    out.append(g[SEVERITY_GLYPH.get(level, "info")], style=ROLES[SEVERITY_ROLE.get(level, "info")])
    out.append(" ")
    out.append(escape(word))
    return out


def box(title: str, rows: list[Text | str | None], mode: Mode, footer: str | None = None,
        max_width: int = 100) -> str:
    """Rounded box with the title notched into the top border. ``None`` rows are dividers."""
    g = glyphs(mode)
    width = max(30, min(mode.width, max_width))
    inner = width - 4  # "│ " + content + " │"
    border = ROLES["border"]

    def notch_line(left: str, right: str, nl: str, nr: str, label: str | None) -> Text:
        line = seg(left + g["h"] + (nl if label else ""), border)  # one span keeps "╭─┐" contiguous
        if label:
            label_t = fit(seg(escape(label), "title"), min(cell_len(escape(label)), width - 6), mode)
            line.append_text(label_t)
            line.append(nr, style=border)
        rest = width - cell_len(line.plain) - 1
        line.append(g["h"] * max(0, rest) + right, style=border)
        return line

    lines = [notch_line(g["tl"], g["tr"], g["nl"], g["nr"], title)]
    for row in rows:
        if row is None:
            lines.append(seg(g["lt"] + g["h"] * (width - 2) + g["rt"], border))
            continue
        content = row if isinstance(row, Text) else Text(row)
        line = seg(g["v"] + " ", border)
        line.append_text(fit(content, inner, mode))
        line.append(" " + g["v"], style=border)
        lines.append(line)
    lines.append(notch_line(g["bl"], g["br"], g["fl"], g["fr"], footer))
    return "\n".join(render(mode, ln) for ln in lines) + "\n"


def table_rows(header: list[str], rows: list[list[Text | str]], mode: Mode,
               right: set[int] | None = None, gap: int = 2) -> list[Text]:
    """Column-aligned rows (header in the title role, no rule under it — like btop's proc list)."""
    right = right or set()
    cells = [[c if isinstance(c, Text) else Text(escape(c)) for c in r] for r in rows]
    widths = [cell_len(h) for h in header]
    for r in cells:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], cell_len(c.plain))
    out = []
    head = Text()
    for i, h in enumerate(header):
        cell = seg(h, "title")
        pad = widths[i] - cell_len(h)
        head.append_text(Text(" " * pad) + cell if i in right else cell + Text(" " * pad))
        if i < len(header) - 1:
            head.append(" " * gap)
    out.append(head)
    for r in cells:
        line = Text()
        for i, c in enumerate(r):
            pad = widths[i] - cell_len(c.plain)
            line.append_text(Text(" " * pad) + c if i in right else c + Text(" " * pad))
            if i < len(r) - 1:
                line.append(" " * gap)
        out.append(line)
    return out


def error(message: str, hint: str | None, mode: Mode) -> str:
    g = glyphs(mode)
    first = Text()
    first.append(f"{g['err']} error:", style=ROLES["err"])
    first.append(" " + escape(message))
    out = render(mode, first) + "\n"
    if hint:
        out += render(mode, seg("hint: " + escape(hint), "dim")) + "\n"
    return out


def line(mode: Mode, level: str, message: str, detail: str | None = None) -> str:
    text = status(level, message, mode)
    if detail:
        text.append("  " + escape(detail), style=ROLES["dim"])
    return render(mode, text) + "\n"


def progress_enabled() -> bool:
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False
