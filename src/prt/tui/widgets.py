"""Dashboard widgets. Rendering is pure (Status/PrinterInfo → rich Text) so it is testable."""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import Static

from prt.models import Ink, PrinterInfo, Severity, Status
from prt.style import escape

# Palette (frontend-design default): black field, white text, gray hairlines, one green accent.
# Status colours are used on text only. Gray is never used for text (4.3:1 on black < AA).
WHITE, GRAY, GREEN = "#FFFFFF", "#6B7280", "#22C55E"
RED, AMBER = "#EF4444", "#F59E0B"
EIGHTHS = " ▁▂▃▄▅▆▇█"


def ink_fill(ink: Ink) -> str:
    """The cartridge's real colour; near-black inks are drawn white so they show on the black field."""
    h = ink.color.lstrip("#")
    try:
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return WHITE
    return WHITE if (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.2 else f"#{h.upper()}"


def render_tanks(inks: list[Ink], height: int = 10, tank_w: int = 6, gap: int = 4,
                 waste: str | None = None) -> Text:
    """Vertical cartridge tanks side by side, filled from the bottom in eighth-cell steps."""
    out = Text()
    if not inks:
        out.append("no ink data reported", style="italic")
        return out
    inner = tank_w - 2
    # cap row
    for i, _ in enumerate(inks):
        out.append(("" if i == 0 else " " * gap) + "▁" * tank_w, style=GRAY)
    out.append("\n")
    for row in range(height, 0, -1):  # top → bottom
        for i, ink in enumerate(inks):
            if i:
                out.append(" " * gap)
            eighths = 0 if ink.level is None else round(ink.level / 100 * height * 8)
            cell_floor = (row - 1) * 8
            fill = max(0, min(8, eighths - cell_floor))
            low_row = ink.level is not None and row == max(1, round(ink.low / 100 * height))
            out.append("▕", style=GRAY)
            out.append(EIGHTHS[fill] * inner, style=ink_fill(ink))
            out.append("▏" if not low_row else "◂", style=AMBER if low_row else GRAY)
        out.append("\n")
    for i, _ in enumerate(inks):
        out.append(("" if i == 0 else " " * gap) + "▔" * tank_w, style=GRAY)
    out.append("\n")
    for i, ink in enumerate(inks):
        out.append(("" if i == 0 else " " * gap) + f"{ink.short:^{tank_w}}", style="bold")
    out.append("\n")
    for i, ink in enumerate(inks):
        pct = "?" if ink.level is None else f"{ink.level}%"
        colour = {"replace": RED, "low": AMBER}.get(ink.state, WHITE)
        out.append(("" if i == 0 else " " * gap) + f"{pct:^{tank_w}}", style=f"bold {colour}")
    out.append("\n")
    for i, ink in enumerate(inks):
        word = {"replace": "✗ out", "low": "▲ low", "unknown": "?"}.get(ink.state, "")
        colour = {"replace": RED, "low": AMBER}.get(ink.state, WHITE)
        out.append(("" if i == 0 else " " * gap) + f"{word:^{tank_w}}", style=colour)
    # The waste ink pad decides whether the printer keeps working: show it with the inks.
    if waste:
        sym, colour, word = {"ok": ("●", GREEN, "ok"), "almost-full": ("▲", AMBER, "almost full — service soon"),
                             "full": ("✗", RED, "full — service required")}.get(waste, ("◆", WHITE, waste))
        out.append("\n\n")
        out.append("waste ink pad  ")
        out.append(f"{sym} ", style=f"bold {colour}")
        out.append(word, style="bold")
    return out


def _uptime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h {rem // 60:02d}m" if d else f"{h}h {rem // 60:02d}m"


def render_system(info: PrinterInfo | None) -> Text:
    out = Text()
    if info is None:
        out.append("waiting for the printer…", style="italic")
        return out
    rows = [
        ("Model", info.make_model), ("Firmware", info.firmware), ("Serial", info.serial),
        ("Uptime", _uptime(info.uptime_seconds)),
        ("Pages", f"{info.page_count:,}" if info.page_count is not None else None),
        ("Host", info.host), ("Driver", info.driver),
        ("Speed", f"{info.ppm} ppm" if info.ppm else None),
        ("Duplex", "yes" if info.duplex else ("no" if info.duplex is not None else None)),
    ]
    first = True
    for k, v in rows:
        if v in (None, ""):
            continue
        if not first:
            out.append("\n")
        first = False
        out.append(f"{k:<10}")
        out.append(escape(v), style="bold")
    return out


def render_paper(st: Status | None) -> Text:
    out = Text()
    if st is None or not st.trays:
        out.append("no media reported", style="italic")
        return out
    for i, tray in enumerate(st.trays):
        if i:
            out.append("\n")
        out.append(f"{escape(tray.source):<7}")
        out.append(escape(tray.media or "?"), style="bold")
        if tray.media_type:
            out.append(f" · {escape(tray.media_type)}")
        out.append("  ✓", style=GREEN)
        if tray.level is not None:
            out.append(f" {tray.level}%")
    if all(t.level is None for t in st.trays):
        out.append("\n")
        out.append("fill level not reported by printer", style="italic")
    return out


def render_alerts(st: Status | None) -> Text:
    out = Text()
    if st is None:
        return out
    if not st.alerts:
        out.append("● ", style=GREEN)
        out.append("all clear")
        return out
    for i, a in enumerate(st.alerts):
        if i:
            out.append("\n")
        sym, colour = {Severity.ERROR: ("✗", RED), Severity.WARNING: ("▲", AMBER),
                       Severity.INFO: ("◆", WHITE)}[a.severity]
        out.append(f"{sym} ", style=f"bold {colour}")
        out.append(escape(a.message))
    return out


def render_topbar(name: str, model: str, st: Status | None, printers: list[tuple[str, bool]],
                  active: int, busy: bool) -> Text:
    out = Text()
    out.append(" prt ", style=f"bold {GREEN}")
    out.append("│ ", style=GRAY)
    out.append(escape(model), style="bold")
    out.append(f"  {escape(name)}  ")
    if st is None:
        out.append("○ connecting…" if busy else "○ unknown")
    elif st.stale:
        out.append("○ offline", style=f"bold {AMBER}")
        out.append(f" · last seen {time.strftime('%H:%M', time.localtime(st.checked_at))}")
    else:
        colour = {"idle": GREEN, "processing": GREEN, "stopped": RED}.get(st.state, WHITE)
        out.append("● ", style=colour)
        out.append(st.state, style="bold")
        if st.queued_jobs:
            out.append(f" · {st.queued_jobs} queued")
    if len(printers) > 1:
        out.append("   ")
        for i, (label, ok) in enumerate(printers):
            style = f"bold {GREEN}" if i == active else ""
            out.append(f" [{i + 1}] {escape(label)}", style=style)
    return out


class Panel(Static):
    """A bordered dashboard card; the border title carries its name."""

    def __init__(self, title: str, **kwargs):
        super().__init__("", **kwargs)
        self.border_title = title
