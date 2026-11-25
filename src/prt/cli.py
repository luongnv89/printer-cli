"""prt — command-line control for network printers."""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import typer
from rich.text import Text

from prt import __version__, cache, guard, render, style
from prt.config import Config
from prt.drivers.base import FINAL_JOB_STATES, KIND_BY_EXIT_CODE, Driver, PrinterError, Unreachable
from prt.drivers.registry import driver_names, make_driver
from prt.maintenance import head_clean_preflight, nozzle_check_preflight
from prt.models import Capability, Job, PrintOptions, ScanSettings, Severity, Status, Target, to_dict
from prt.style import ROLES, Mode, t

if TYPE_CHECKING:
    from prt.discovery import Discovered

# --- App state ----------------------------------------------------------------


class Fmt(str, Enum):
    text = "text"
    json = "json"


@dataclass
class State:
    printer: str | None = None
    fmt: Fmt = Fmt.text
    verbose: bool = False
    quiet: bool = False
    timeout: float | None = None
    config_path: str | None = None
    no_color: bool = False
    out: Mode = field(default_factory=lambda: style.PLAIN)
    err: Mode = field(default_factory=lambda: style.PLAIN)
    _cfg: Config | None = None
    _driver: Driver | None = None

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            self._cfg = Config.load(self.config_path)
        return self._cfg

    @property
    def json(self) -> bool:
        return self.fmt == Fmt.json

    def driver(self) -> Driver:
        if self._driver is None:
            target = self.cfg.resolve(self.printer)
            self._driver = make_driver(target, self.timeout or self.cfg.timeout)
        return self._driver

    # output helpers
    def write(self, s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    def note(self, level: str, message: str, detail: str | None = None) -> None:
        if not self.quiet:
            sys.stderr.write(style.line(self.err, level, message, detail))

    def emit(self, data: Any, text: Callable[[], str], plain: Callable[[], list[str]]) -> None:
        if self.json:
            self.write(json.dumps(data, indent=2, default=str) + "\n")
        elif self.out.styled:
            self.write(text())
        else:
            self.write("".join(line + "\n" for line in plain()))


STATE = State()

app = typer.Typer(
    name="prt",
    help="Control network printers from the terminal: status, ink, maintenance, printing and scanning.",
    epilog="Examples:  prt status  ·  prt ink -f json  ·  prt scan -o doc.pdf  ·  prt tui",
    no_args_is_help=True,
    add_completion=True,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
printers_app = typer.Typer(help="Manage saved printers.", no_args_is_help=True)
maintain_app = typer.Typer(help="Maintenance: nozzle check, head cleaning, test page, identify.",
                           no_args_is_help=True)
jobs_app = typer.Typer(help="List, wait for or cancel print jobs.", invoke_without_command=True)
skills_app = typer.Typer(help="List the agent skills that come with prt, or install them for Claude Code and "
                              "other agents.", no_args_is_help=True)
app.add_typer(printers_app, name="printers")
app.add_typer(maintain_app, name="maintain")
app.add_typer(jobs_app, name="jobs")
puzzle_app = typer.Typer(help="Puzzle pages to print: a sudoku, a word search, a crossword or a maze, one per A4 "
                              "page, with the solution kept for checking.", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
app.add_typer(puzzle_app, name="puzzle")


def _version(value: bool) -> None:
    if value:
        sys.stdout.write(f"prt {__version__}\n")
        raise typer.Exit()


@app.callback()
def root(
    printer: Optional[str] = typer.Option(None, "--printer", "-p", envvar="PRT_PRINTER",
                                          help="Saved printer name, hostname or IP (or 'demo')."),
    fmt: Fmt = typer.Option(Fmt.text, "--format", "-f", help="Output format."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colour (also: NO_COLOR=1)."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show protocol details on stderr."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print results, no notes."),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=0.5, help="Network timeout in seconds (default 5)."),
    config: Optional[str] = typer.Option(None, "--config", envvar="PRT_CONFIG", help="Config file path."),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True,
                                 help="Show the version and exit."),
) -> None:
    STATE._cfg, STATE._driver = None, None  # fresh per invocation (tests call main() repeatedly)
    STATE._stale_exit = False  # type: ignore[attr-defined]
    STATE.printer, STATE.fmt, STATE.verbose, STATE.quiet = printer, fmt, verbose, quiet
    STATE.timeout, STATE.config_path, STATE.no_color = timeout, config, no_color
    from prt.transport import ipp as _ipp
    _ipp.TRACE = verbose
    STATE.out = style.decide(sys.stdout, machine=fmt == Fmt.json, no_color=no_color, is_stdout=True)
    STATE.err = style.decide(sys.stderr, no_color=no_color, is_stdout=False)


# --- Formatting helpers ------------------------------------------------------------


def _uptime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h{m:02d}m" if d else f"{h}h{m:02d}m"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


INK_STATE_WORD = {"ok": ("ok", ""), "low": ("warn", "low"), "replace": ("err", "replace"),
                  "unknown": ("info", "unknown")}
SEV_LEVEL = {Severity.ERROR: "err", Severity.WARNING: "warn", Severity.INFO: "info"}


def _state_level(st: Status) -> str:
    if st.state == "offline":
        return "offline"
    if st.state == "stopped":
        return "err"
    return "ok"


def _ink_rows(st: Status, mode: Mode, inner: int) -> list[Text]:
    name_w = max([len("ink")] + [len(i.name.replace(" ink", "")) for i in st.inks]) + 2
    meter_w = max(10, min(40, inner - name_w - 5 - 12))
    rows = []
    for ink in st.inks:
        row = Text(f"{style.escape(ink.name.replace(' ink', '')):<{name_w}}")
        row.append_text(style.meter(ink.level, meter_w, mode, style.ink_style(ink.color)))
        level, word = INK_STATE_WORD[ink.state]
        if word:
            row.append("  ")
            row.append_text(style.status(level, word, mode))
        rows.append(row)
    return rows


def render_status(st: Status, name: str, model: str, mode: Mode, firmware: str | None = None,
                  uptime: int | None = None, pages: int | None = None) -> str:
    g = style.glyphs(mode)
    inner = max(30, min(mode.width, 100)) - 4
    head = style.status(_state_level(st), st.state, mode)
    facts = []
    if firmware:
        facts.append(f"firmware {firmware}")
    if uptime is not None:
        facts.append(f"up {_uptime(uptime)}")
    if pages is not None:
        facts.append(f"{pages:,} pages")
    if st.queued_jobs:
        facts.append(f"{st.queued_jobs} queued")
    if st.stale:
        facts.append(f"last seen {_clock(st.checked_at)} (cached)")
    if facts:
        head.append("    " + f" {g['dot']} ".join(style.escape(f) for f in facts), style=ROLES["dim"])
    rows: list[Text | None] = [head, None, style.seg("ink", "title")]
    rows += _ink_rows(st, mode, inner)
    rows.append(None)
    paper = style.seg("paper   ", "title")
    if st.trays:
        for i, tray in enumerate(st.trays):
            if i:
                paper.append("   ")
            paper.append(style.escape(tray.source) + " ", style=ROLES["dim"])
            paper.append(style.escape(tray.media or "?"))
            paper.append(" " + g["check"], style=ROLES["ok"])
            if tray.level is not None:
                paper.append(f" {tray.level}%")
        if all(tr.level is None for tr in st.trays):
            paper.append("   (levels not reported)", style=ROLES["dim"])
    else:
        paper.append("no media reported", style=ROLES["dim"])
    rows.append(paper)
    waste = style.seg("waste   ", "title")
    waste_word = {"ok": ("ok", "ok"), "almost-full": ("warn", "almost full — service soon"),
                  "full": ("err", "full — service required"), None: ("info", "not reported")}
    wl, ww = waste_word.get(st.waste, ("info", st.waste or "?"))
    waste.append_text(style.status(wl, ww + (f" ({st.waste_level}%)" if st.waste_level is not None else ""), mode))
    rows.append(waste)
    other = [a for a in st.alerts if not a.code.startswith(("ink-", "marker-waste"))]
    if other:
        rows.append(None)
        for a in other:
            r = style.seg("alert   ", "title")
            r.append_text(style.status(SEV_LEVEL[a.severity], a.message, mode))
            rows.append(r)
    n_alerts = len(st.alerts)
    footer = (f"{n_alerts} alert{'s' if n_alerts != 1 else ''} {g['dot']} "
              f"{'cached ' if st.stale else 'checked '}{_clock(st.checked_at)}")
    return style.box(f"{model} {g['dot']} {name}", rows, mode, footer=footer)


def status_plain(st: Status) -> list[str]:
    out = [style.plain_record("state", st.state, "stale" if st.stale else "live")]
    out += [style.plain_record("ink", i.name, "" if i.level is None else i.level, i.state) for i in st.inks]
    out += [style.plain_record("tray", tr.source, tr.media or "", tr.media_type or "",
                               "" if tr.level is None else tr.level) for tr in st.trays]
    out.append(style.plain_record("waste", st.waste or "unknown"))
    out += [style.plain_record("alert", a.severity.value, a.message) for a in st.alerts]
    return out


def _fetch_status(d: Driver | None = None) -> tuple[Driver | None, Status]:
    """Live status, or the cached one when the printer does not answer (even during auto-detection)."""
    target = d.target if d else STATE.cfg.resolve(STATE.printer)
    try:
        d = d or STATE.driver()
        st = d.status()
    except Unreachable:
        cached = cache.load(target.name)
        if cached is None:
            raise
        STATE.note("warn", f"{target.name} is not answering — showing last known status",
                   f"from {time.strftime('%Y-%m-%d %H:%M', time.localtime(cached.checked_at))}")
        STATE._stale_exit = True  # type: ignore[attr-defined]
        return d, cached
    cache.save(target.name, st)
    return d, st


def _quick_facts(d: Driver | None) -> tuple[str, str | None, int | None, int | None]:
    """Model, firmware, uptime (already-fetched attributes) and page count (SNMP, when available)."""
    from prt.drivers.ipp import IPPDriver
    if d is None:
        return STATE.cfg.resolve(STATE.printer).name, None, None, None
    pages = None
    if d.supports(Capability.COUNTERS) and hasattr(d, "snmp_page_count"):
        pages = d.snmp_page_count()  # type: ignore[attr-defined]
    if isinstance(d, IPPDriver) and d._attrs_cache:
        info = d.parse_info(d._attrs_cache)
        return info.make_model, info.firmware, info.uptime_seconds, pages
    return d.display_name, None, None, pages


def _exit_with(code: int, kind: str, message: str, hint: str | None = None) -> None:
    """End a command that already said what happened: no text error, but the JSON error object under -f json."""
    if STATE.json:
        sys.stderr.write(_error_json(PrinterError(message, hint, exit_code=code, kind=kind)))
    raise typer.Exit(code)


def _finish_stale() -> None:
    if getattr(STATE, "_stale_exit", False):
        _exit_with(1, "stale", "the printer is not answering — this is its last known status",
                   "wake the printer (press a button) and retry")


# --- Read commands -------------------------------------------------------------------


@app.command()
def status(
    watch: Optional[float] = typer.Option(None, "--watch", "-w", min=2,
                                          help="Repeat every N seconds, one line per sample (Ctrl-C to stop)."),
) -> None:
    """State, alerts, ink levels and paper — what the printer's panel should be telling you."""
    if watch:
        _watch(STATE.driver(), watch)
        return
    d, st = _fetch_status()
    name = STATE.cfg.resolve(STATE.printer).name
    model, fw, up, pages = _quick_facts(d) if not st.stale else (name, None, None, None)
    STATE.emit(
        {"printer": name, "model": model, "page_count": pages, **to_dict(st)},
        lambda: render_status(st, name, model, STATE.out, fw, up, pages),
        lambda: status_plain(st),
    )
    _finish_stale()


def _watch(d: Driver, every: float) -> None:
    while True:
        try:
            st = d.status()
            cache.save(d.target.name, st)
        except Unreachable:
            st = Status(state="offline", accepting=False)
        stamp = time.strftime("%H:%M:%S")
        if STATE.json:
            STATE.write(json.dumps({"time": stamp, **to_dict(st)}, default=str) + "\n")
        elif STATE.out.styled:
            line = style.seg(stamp + "  ", "dim")
            line.append_text(style.status(_state_level(st), f"{st.state:<10}", STATE.out))
            for ink in st.inks:
                line.append(f" {ink.short} ", style=ROLES["dim"])
                line.append(f"{ink.level if ink.level is not None else '?':>3}",
                            style=style.ink_style(ink.color) if STATE.out.color else "")
            line.append(f"   {len(st.alerts)} alerts", style=ROLES["dim"])
            STATE.write(style.render(STATE.out, line) + "\n")
        else:
            STATE.write(style.plain_record(stamp, st.state,
                                           *[f"{i.short}={i.level}" for i in st.inks],
                                           f"alerts={len(st.alerts)}") + "\n")
        time.sleep(every)


@app.command()
def ink() -> None:
    """Ink levels only (compact)."""
    _, st = _fetch_status()
    name = STATE.cfg.resolve(STATE.printer).name
    mode = STATE.out

    def text() -> str:
        inner = max(30, min(mode.width, 100)) - 4
        g = style.glyphs(mode)
        foot = f"{'cached ' if st.stale else ''}{_clock(st.checked_at)}"
        return style.box(f"ink {g['dot']} {name}", list(_ink_rows(st, mode, inner)), mode, footer=foot)

    STATE.emit({"printer": name, "stale": st.stale, "inks": [to_dict(i) for i in st.inks]},
               text, lambda: [style.plain_record(i.name, "" if i.level is None else i.level, i.state)
                              for i in st.inks])
    _finish_stale()


@app.command()
def overview() -> None:
    """System, software and hardware: model, serial, firmware, uptime, counters, capabilities."""
    d = STATE.driver()
    info = d.info()
    scanner: dict[str, Any] = {}
    if d.supports(Capability.SCAN):
        scanner = _scanner_info(d)
    info.scanner = scanner
    pairs: list[tuple[str, list[tuple[str, Any]]]] = [
        ("system", [("model", info.make_model), ("vendor", info.vendor), ("serial", info.serial),
                    ("uuid", info.uuid), ("driver", info.driver)]),
        ("software", [("firmware", info.firmware), ("ipp", ", ".join(info.ipp_versions)),
                      ("formats", ", ".join(f.removeprefix("image/").removeprefix("application/")
                                            for f in info.formats)),
                      ("admin page", info.admin_url)]),
        ("hardware", [("uptime", _uptime(info.uptime_seconds)),
                      ("pages printed", f"{info.page_count:,}" if info.page_count is not None else None),
                      ("colour", _yn(info.color)), ("duplex", _yn(info.duplex)),
                      ("speed", f"{info.ppm} ppm" if info.ppm else None),
                      ("resolutions", ", ".join(info.resolutions)),
                      ("trays", ", ".join(s for s in info.media_sources if s != "auto")),
                      ("media", ", ".join(info.media[:10]) + (" …" if len(info.media) > 10 else ""))]),
        ("network", [("host", info.host), ("uris", "  ".join(info.uris))]),
    ]
    if scanner:
        pairs.append(("scanner", [("source", ", ".join(scanner.get("sources", []))),
                                  ("resolutions", ", ".join(f"{r}" for r in scanner.get("resolutions", [])) + " dpi"),
                                  ("modes", ", ".join(scanner.get("color_modes", []))),
                                  ("formats", ", ".join(scanner.get("formats", [])))]))
    pairs.append(("capabilities", [("supported", ", ".join(c.replace("_", "-") for c in info.capabilities))]))
    mode = STATE.out

    def text() -> str:
        rows: list[Text | None] = []
        key_w = 15
        for i, (section, items) in enumerate(pairs):
            if i:
                rows.append(None)
            rows.append(style.seg(section, "title"))
            for k, v in items:
                if v in (None, ""):
                    continue
                r = style.seg(f"  {k:<{key_w}}", "dim")
                r.append(style.escape(v))
                rows.append(r)
        return style.box(f"{info.make_model} {style.glyphs(mode)['dot']} {info.name}", rows, mode,
                         footer=f"driver {info.driver}")

    def plain() -> list[str]:
        return [style.plain_record(section, k, v) for section, items in pairs for k, v in items
                if v not in (None, "")]

    STATE.emit(to_dict(info), text, plain)


def _yn(v: bool | None) -> str | None:
    return None if v is None else ("yes" if v else "no")


def _scanner_info(d: Driver) -> dict[str, Any]:
    if hasattr(d, "scanner_info"):
        return d.scanner_info()  # type: ignore[attr-defined]
    from prt.transport.escl import ESCLClient
    try:
        caps = ESCLClient.for_target(d.target, min(d.timeout, 4)).capabilities()
    except Exception:
        return {}
    return {"make_model": caps.make_model, "sources": caps.sources, "resolutions": caps.resolutions,
            "color_modes": caps.color_modes, "formats": caps.formats}


app.command("info", hidden=True, help="Alias for overview.")(overview)


def _slug(name: str) -> str:
    """A bare TOML key: mDNS names can hold characters a config table name cannot."""
    return re.sub(r"[^a-z0-9_]+", "-", name.lower()).strip("-") or "printer"


def _save_discovered(cfg: Config, found: list[Discovered]) -> list[tuple[str | None, bool]]:
    """Save each newly found IPP printer; per device, in order: (saved name or None, added now)."""
    by_host = {p.host.lower(): n for n, p in cfg.printers.items()}
    taken = {*cfg.printers, "demo"}  # a saved "demo" would shadow the built-in replay printer
    out: list[tuple[str | None, bool]] = []
    for f in found:
        keys = [k.lower() for k in (f.host, *f.addresses)]
        saved = next((by_host[k] for k in keys if k in by_host), None)
        # TXT records are untrusted: a control character would make the config unloadable.
        if saved or not f.ipp or not all(s.isprintable() for s in (f.host, f.ipp_path, f.scan_path)):
            out.append((saved, False))
            continue
        base = name = _slug(f.suggested_name())
        n = 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        cfg.printers[name] = f.to_target(name)
        taken.add(name)
        by_host.update(dict.fromkeys(keys, name))
        out.append((name, True))
    return out


@app.command()
def discover(timeout: float = typer.Option(3.0, "--wait", help="Seconds to listen for mDNS answers.")) -> None:
    """Find printers and scanners on the local network (mDNS / Bonjour) and save new printers."""
    from prt.discovery import discover as run
    if STATE.err.styled and not STATE.quiet:
        sys.stderr.write(style.render(STATE.err, Text(f"listening for printers for {timeout:g}s…",
                                                      style=ROLES["dim"])) + "\n")
    found = run(timeout)
    if not found:
        STATE.note("warn", "no printers answered",
                   "a sleeping printer does not reply to discovery — wake it (press a button) and retry")
    cfg = STATE.cfg
    results = _save_discovered(cfg, found)
    added = [name for name, new in results if new]
    hint = None
    if added:
        cfg.save()
        if not cfg.default and len(cfg.printers) > 1:
            hint = f"pick a default: prt printers default {added[0]}"
        STATE.note("ok", f"added {len(added)} printer{'s' if len(added) > 1 else ''}: {', '.join(added)}",
                   hint or str(cfg.path))
    mode = STATE.out

    def text() -> str:
        if not found:
            return style.box("discover", [style.seg("no printers answered", "dim")], mode,
                             footer="0 found")
        rows = [[f.make_model or f.name, f.host, f.addresses[0] if f.addresses else "",
                 " ".join(f.services), name or ""] for f, (name, _) in zip(found, results)]
        body: list[Text | None] = [*style.table_rows(["model", "host", "address", "services", "saved as"],
                                                     rows, mode)]
        if added:
            body += [None, style.seg("added ", "ok") + t(", ".join(added))]
        if hint:
            body.append(style.seg(hint, "dim"))
        return style.box("discover", body, mode, footer=f"{len(found)} found {style.glyphs(mode)['dot']} {len(added)} added")

    STATE.emit([f.__dict__ | {"services": f.services, "saved_as": name, "added": new}
                for f, (name, new) in zip(found, results)], text,
               lambda: [style.plain_record(f.make_model or f.name, f.host,
                                           f.addresses[0] if f.addresses else "", ",".join(f.services),
                                           name or "", "added" if new else "")
                        for f, (name, new) in zip(found, results)])


# --- printers ---------------------------------------------------------------------------


MAX_PARALLEL_CHECKS = 8


def _check_printers(targets: list[Target], timeout: float) -> list[str]:
    """'online' or 'offline' for each target, in the same order; the checks run in parallel."""
    def check(tgt: Target) -> str:
        try:
            make_driver(tgt, timeout).status()
            return "online"
        except Exception:  # any failure (DNS, refused, bad reply, unknown driver) means not connected
            return "offline"

    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CHECKS, len(targets))) as pool:
        return list(pool.map(check, targets))


CHECK_OPTION_HELP = "Check each printer's connection (online/offline); --no-check skips it."


@printers_app.command("list")
def printers_list(check: bool = typer.Option(True, "--check/--no-check", help=CHECK_OPTION_HELP)) -> None:
    """Saved printers, their driver, which one is the default and their connection status."""
    cfg = STATE.cfg
    targets = list(cfg.printers.values())
    states: list[str | None] = [None] * len(targets)
    if check:
        states = list(_check_printers(targets, STATE.timeout or min(cfg.timeout, 3)))
    rows_data = [{"name": name, "host": tgt.host, "driver": tgt.driver,
                  "default": name == cfg.default, "reachable": reach}
                 for (name, tgt), reach in zip(cfg.printers.items(), states)]
    mode = STATE.out

    def text() -> str:
        if not rows_data:
            return style.box("printers", [style.seg("no saved printers — run `prt discover`", "dim")],
                             mode, footer=str(cfg.path))
        g = style.glyphs(mode)
        rows = []
        for r in rows_data:
            mark = style.seg(g["ok"] if r["default"] else " ", "accent")
            reach = (style.status("ok" if r["reachable"] == "online" else "offline", r["reachable"], mode)
                     if r["reachable"] else Text(""))
            rows.append([mark, t(r["name"]), t(r["host"]), t(r["driver"]), reach])
        return style.box("printers", style.table_rows(["", "name", "host", "driver", "state"], rows, mode),
                         mode, footer=f"{len(rows_data)} saved {g['dot']} {g['ok']} default")

    STATE.emit(rows_data, text, lambda: [style.plain_record(r["name"], r["host"], r["driver"],
                                                            "default" if r["default"] else "",
                                                            r["reachable"] or "") for r in rows_data])


@app.command("list")
def list_cmd(check: bool = typer.Option(True, "--check/--no-check", help=CHECK_OPTION_HELP)) -> None:
    """Saved printers and their connection status (same as prt printers list)."""
    printers_list(check=check)


@printers_app.command("add")
def printers_add(
    name: str = typer.Argument(..., help="Short name to refer to this printer."),
    host: str = typer.Argument(..., help="Hostname or IP, e.g. EPSON721655.local or 192.168.1.70."),
    driver: str = typer.Option("auto", "--driver", "-d", help="auto, ipp, epson or fake."),
    default: bool = typer.Option(False, "--default", help="Make it the default printer."),
    port: int = typer.Option(631, "--port", help="IPP port."),
) -> None:
    """Save a printer under a short name."""
    if driver not in driver_names():
        raise PrinterError(f"unknown driver '{driver}'", hint="choose: " + ", ".join(driver_names()), exit_code=2)
    from prt.models import Target
    cfg = STATE.cfg
    cfg.printers[name] = Target(name=name, host=host, driver=driver, ipp_port=port)
    if default or not cfg.default:
        cfg.default = name
    cfg.save()
    STATE.note("ok", f"saved '{name}' → {host}", f"{'default · ' if cfg.default == name else ''}{cfg.path}")
    if STATE.json:
        STATE.write(json.dumps({"name": name, "host": host, "driver": driver, "default": cfg.default == name}) + "\n")


@printers_app.command("remove")
def printers_remove(name: str = typer.Argument(..., help="Saved printer name.")) -> None:
    """Forget a saved printer."""
    cfg = STATE.cfg
    if name not in cfg.printers:
        raise PrinterError(f"no saved printer named '{name}'", hint="see `prt printers list`", exit_code=2)
    del cfg.printers[name]
    if cfg.default == name:
        cfg.default = next(iter(cfg.printers), None)
    cfg.save()
    STATE.note("ok", f"removed '{name}'")


@printers_app.command("default")
def printers_default(name: str = typer.Argument(..., help="Saved printer name.")) -> None:
    """Choose the printer used when --printer is not given."""
    cfg = STATE.cfg
    if name not in cfg.printers:
        raise PrinterError(f"no saved printer named '{name}'", hint="see `prt printers list`", exit_code=2)
    cfg.default = name
    cfg.save()
    STATE.note("ok", f"'{name}' is now the default printer")


# --- maintain --------------------------------------------------------------------------------


def _confirm(question: str, yes: bool) -> None:
    guard.require_consent(yes)  # agent mode: --yes, never a prompt
    if yes:
        return
    if not sys.stdin.isatty():
        raise PrinterError("confirmation needed but stdin is not a terminal", hint="re-run with --yes",
                           exit_code=2, kind="confirmation_required")
    if not typer.confirm(question, default=False, err=True):
        STATE.note("info", "cancelled — nothing was sent")
        _exit_with(1, "declined", "cancelled — nothing was sent")


def _authorize(pages: int | None, yes: bool, question: str | None):
    """Run the print guard and the confirmation prompt; returns the ledger reservation to send under.

    Agent-mode consent and the page caps are checked before the prompt, so a print that would be refused is
    never asked about. ``question`` None skips the prompt (``prt print`` asks nothing outside agent mode).
    """
    guard.check(STATE.cfg, pages, yes=yes)
    if question:
        _confirm(question, yes)
    return guard.reserve(STATE.cfg, pages)


def _show_preflight(pf) -> None:
    for b in pf.blockers:
        STATE.note("err", b)
    for w in pf.warnings:
        STATE.note("warn", w)


@maintain_app.command("nozzle-check")
def nozzle_check(yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Print the nozzle check pattern (1 sheet, a little ink). Gaps in a line = clogged nozzles."""
    d = STATE.driver()
    d.require(Capability.NOZZLE_CHECK)
    pf = nozzle_check_preflight(_fetch_status(d)[1])
    _show_preflight(pf)
    if not pf.ok:
        raise PrinterError("nozzle check not sent", hint="fix the issues above and retry", kind="refused")
    with _authorize(1, yes, f"Print a nozzle check on {d.display_name}?") as ticket:
        ticket.attempt(1)
        d.nozzle_check()
    STATE.note("ok", "nozzle check sent", "broken lines in the pattern → run `prt maintain clean`")
    if STATE.json:
        STATE.write(json.dumps({"action": "nozzle_check", "sent": True}) + "\n")


@maintain_app.command("clean")
def head_clean(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    force: bool = typer.Option(False, "--force", help="Clean even if ink is low or the waste pad is near full."),
) -> None:
    """Clean the print head. Uses ink and fills the waste pad — run a nozzle check first."""
    d = STATE.driver()
    d.require(Capability.HEAD_CLEAN)
    pf = head_clean_preflight(_fetch_status(d)[1])
    _show_preflight(pf)
    if not pf.ok and not force:
        raise PrinterError("head cleaning refused for safety",
                           hint="replace low cartridges first, or pass --force if you understand the risk",
                           kind="refused")
    _confirm(f"Start head cleaning on {d.display_name}? (takes ~1 minute, uses ink)", yes)
    d.head_clean()
    STATE.note("ok", "head cleaning started", "wait until the printer is quiet, then run a nozzle check")
    if STATE.json:
        STATE.write(json.dumps({"action": "head_clean", "sent": True, "forced": force and not pf.ok}) + "\n")


@maintain_app.command("test-page")
def test_page(yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Print prt's colour test page: ink bars, gradients, grey steps, fine lines (A4)."""
    d = STATE.driver()
    d.require(Capability.TEST_PAGE)
    with _authorize(1, yes, f"Print a colour test page on {d.display_name}?") as ticket:
        ticket.attempt(1)
        job = d.test_page()
    STATE.note("ok", f"test page queued as job {job}")
    if STATE.json:
        STATE.write(json.dumps({"action": "test_page", "job_id": job}) + "\n")


@maintain_app.command("identify")
def identify() -> None:
    """Flash the printer's panel so you can find it."""
    d = STATE.driver()
    d.require(Capability.IDENTIFY)
    d.identify()
    STATE.note("ok", f"{d.display_name} should be flashing now")
    if STATE.json:
        STATE.write(json.dumps({"action": "identify", "sent": True}) + "\n")


def _require_keepalive_ready(d: Driver) -> None:
    """Refuse the keep-alive patch on an offline or busy printer, as the nozzle check does (exit code 1)."""
    pf = nozzle_check_preflight(_fetch_status(d)[1])
    _show_preflight(pf)
    if not pf.ok:
        raise PrinterError("keep-alive patch not sent", hint="fix the issues above and retry", kind="refused")


@app.command("keepalive")
def keepalive_cmd(
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the page here instead of printing; the extension picks the "
                                                "file type: .pdf, .png, .jpg or .jpeg."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the page here (.pdf, .png, .jpg or .jpeg) "
                                                   "before printing it; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt (needed when a scheduler runs "
                                                        "it)."),
) -> None:
    """Keep-alive patch: a small square of every ink colour on one A4 sheet, to stop the nozzles drying and clogging.

    Print it every few days from cron, a systemd timer or launchd with --yes. Cyan, magenta, yellow and black always
    print; light and grey tints let a printer with light or photo inks use them if its driver chooses to.
    """
    from prt import booth, keepalive
    # Check the file names before a file is written or a driver exists.
    copies = [(path, booth.format_for(path)) for path in save or []]
    if preview:
        booth.format_for(preview)
    rendered = render.Rendered([keepalive.compose()])
    _print_sheet("keepalive", rendered, keepalive.KIND, preview, copies, "Print a keep-alive colour patch",
                 "prt keepalive", yes, {"inks": list(keepalive.INKS),
                                        "patches": [name for row in keepalive.PATCHES for name, _ in row]},
                 precheck=_require_keepalive_ready)


# --- print / jobs / scan -------------------------------------------------------------------


JOB_LEVEL = {"completed": "ok", "processing": "info", "pending": "info", "canceled": "warn", "aborted": "err"}
WAIT_TIMEOUT = 600.0  # seconds `--wait` and `prt jobs wait` wait for one job by default


class Quality(str, Enum):
    draft = "draft"
    normal = "normal"
    high = "high"


@app.command("print")
def print_cmd(
    files: list[Path] = typer.Argument(..., help="Files to print (JPEG/PNG direct; PDF and others via CUPS)."),
    copies: int = typer.Option(1, "--copies", "-n", min=1, max=99, help="Number of copies."),
    media: Optional[str] = typer.Option(None, "--media", "-m", help="Paper size: a4, letter, 4x6, 5x7, …"),
    mono: bool = typer.Option(False, "--mono", help="Print in black & white."),
    quality: Optional[Quality] = typer.Option(None, "--quality", help="draft, normal or high."),
    duplex: bool = typer.Option(False, "--duplex", help="Print on both sides."),
    tray: Optional[str] = typer.Option(None, "--tray", "-t", help="Input tray: main, photo, disc."),
    direct: bool = typer.Option(False, "--direct", help="Send straight to the printer, never via CUPS."),
    wait: bool = typer.Option(False, "--wait", help="Wait until each job has finished and report how it ended; "
                                                    "exits non-zero when a job fails or is cancelled."),
    wait_timeout: float = typer.Option(WAIT_TIMEOUT, "--wait-timeout", min=1, metavar="SECONDS",
                                       help="With --wait: give up on a job after this many seconds."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the print; needed in agent mode (PRT_AGENT=1)."),
) -> None:
    """Print one or more files."""
    d = STATE.driver()
    d.require(Capability.PRINT)
    if wait:
        from prt.printing import sent_directly
        d.require(Capability.JOBS)
        # Checked before anything is sent: a job handed to CUPS has an id the printer does not know.
        via_cups = [f for f in files if f.is_file() and not sent_directly(f)]
        if via_cups:
            raise PrinterError(f"--wait cannot follow {via_cups[0].name}: only images are sent to the printer "
                               "directly, other files go through CUPS", exit_code=2,
                               hint="convert it to JPEG, or print it without --wait and follow it with `lpstat`")
    results = []
    with _authorize(guard.job_pages(files, copies), yes, None) as ticket:
        for f in files:
            opts = PrintOptions(copies=copies, media=media, mono=mono, quality=quality.value if quality else None,
                                duplex=duplex, tray=tray, direct=direct, job_name=f.name)
            ticket.attempt(guard.job_pages([f], copies) or 0)
            job = d.print_file(f, opts)
            results.append({"file": str(f), "job_id": job})
            STATE.note("ok", f"{f.name} → job {job}" if job else f"{f.name} sent", d.display_name)
    failure = None
    if wait:
        finished = []
        try:
            for r in results:
                job = _await_job(d, r["job_id"], wait_timeout)
                r["state"] = job.state
                finished.append(job)
        except (PrinterError, KeyboardInterrupt):
            if STATE.json:  # every job was sent: name them all, with the state reached so far (or null)
                STATE.write(json.dumps([{"state": None, **r} for r in results]) + "\n")
            raise
        failure = _job_failure(finished)
    if STATE.json:
        STATE.write(json.dumps(results) + "\n")
    if failure:
        raise failure


class BoothStyle(str, Enum):
    metro = "metro"
    daily = "daily"
    wizard = "wizard"
    wanted = "wanted"
    certificate = "certificate"
    sports = "sports"
    missing = "missing"
    movie = "movie"


@app.command("booth")
def booth_cmd(
    photo: Path = typer.Argument(..., help="Photo of a person: JPEG, PNG, WebP, GIF, BMP or TIFF."),
    paper: BoothStyle = typer.Option(BoothStyle.metro, "--style", "-s",
                                     help="metro (colour tabloid, 2 columns), daily (black-and-white "
                                          "broadsheet, 3 columns), wizard (ornate trading card), wanted "
                                          "(weathered pirate poster), certificate (award certificate), sports "
                                          "(sports trading card), missing (missing-pet poster) or movie (movie "
                                          "poster)."),
    story_file: Optional[Path] = typer.Option(None, "--story-file",
                                              help="JSON story: headline and story required; kicker, caption, "
                                                   "byline optional. Use - to read it from stdin."),
    headline: Optional[str] = typer.Option(None, "--headline",
                                           help="Headline; the name on the cards and posters, the film title "
                                                "on movie (overrides the story file)."),
    story: Optional[str] = typer.Option(None, "--story",
                                        help="Story text: the lore, description, citation or stats on the "
                                             "cards and posters, the credits on movie; a newline starts a new "
                                             "paragraph."),
    kicker: Optional[str] = typer.Option(None, "--kicker",
                                         help="Small label above the headline; the card type on wizard, the "
                                              "'Dead or alive' line on wanted, the title on certificate, the team "
                                              "on sports, the question on missing, the tagline on movie."),
    caption: Optional[str] = typer.Option(None, "--caption",
                                          help="Line under the photo; the subtitle on wizard, the reward line on "
                                               "wanted and missing, the award on certificate, the position on "
                                               "sports, the release line on movie."),
    byline: Optional[str] = typer.Option(None, "--byline",
                                         help="Author line, e.g. 'By Jo Smith'; the footer on the cards and "
                                              "posters, the signature on certificate, the tear-off tabs on "
                                              "missing, the rating on movie."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the page here instead of printing; the extension picks "
                                                "the file type: .pdf, .png, .jpg or .jpeg."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the page here (.pdf, .png, .jpg or "
                                                   ".jpeg) before printing it; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Photo booth: print a photo as a newspaper front page, a card, a certificate or a poster (A4).

    Without a story each style uses its own built-in one. Story flags override --story-file.
    """
    from prt import booth
    # Decode and check every input before a file is written or a driver exists, so a bad file name or
    # bad input never leaves a partial result or reaches the printer.
    preview_format = booth.format_for(preview) if preview else None
    copies = [(path, booth.format_for(path)) for path in save or []]
    image = booth.load_photo(photo)
    text = render.booth_story(paper.value, booth.load_story(story_file) if story_file else None,
                              headline=headline, story=story, kicker=kicker, caption=caption, byline=byline)
    rendered = render.booth_page(image, text, paper.value)
    page, cut = rendered.pages[0], rendered.cut
    if cut:
        STATE.note("warn", f"{', '.join(cut)} cut to fit the {paper.value} page",
                   "shorten it, or check the page with --preview")
    if preview:
        preview_size = _save_booth_page(booth.to_bytes(page, preview_format), preview)
        STATE.note("ok", f"saved {preview}", f"{paper.value} page · {preview_format.upper()} · "
                                             f"{preview_size / 1024:,.0f} KiB · not printed")
    # Copies are written before connecting, so they exist even when the printer is unreachable.
    saved = []
    for path, fmt in copies:
        size = _save_booth_page(booth.to_bytes(page, fmt), path)
        STATE.note("ok", f"saved {path}", f"{paper.value} page · {fmt.upper()} · {size / 1024:,.0f} KiB")
        saved.append({"file": str(path), "format": fmt, "bytes": size})
    extra = {"saved": saved} if save else {}
    report = {**rendered.fit(), **rendered.estimate()} if STATE.json else {}
    if preview:
        if STATE.json:
            STATE.write(json.dumps({"action": "booth", "style": paper.value, "file": str(preview),
                                    "bytes": preview_size, **report, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    with _authorize(1, yes, f"Print {photo.name} as a {booth.KINDS[paper.value]} on {d.display_name}?") as ticket:
        job, = render.send_pages(d, [booth.to_jpeg(page)], [f"prt booth {photo.name}"], ticket)
    STATE.note("ok", f"booth page queued as job {job}", d.display_name)
    if STATE.json:
        STATE.write(json.dumps({"action": "booth", "style": paper.value, "job_id": job, **report, **extra}) + "\n")


_fit_report = render.fit_report  # the fit report of booth and gazette JSON lines


def _estimate(pages: list[Any]) -> dict[str, Any]:
    """The ``estimate`` a booth, article or gazette JSON line carries: pages, colour use and ink use of the
    rendered pages, e.g. ``"summary": "~3 pages, colour, est. ink 4%"`` (see ``guard.estimate``)."""
    return {"estimate": guard.estimate(pages)}


def _save_booth_page(data: bytes, path: Path) -> int:
    try:
        path.write_bytes(data)
    except OSError as exc:
        raise PrinterError(f"cannot write {path}: {exc.strerror}",
                           hint="check that the folder exists and is writable") from exc
    return len(data)


@app.command("card")
def card_cmd(
    photo: Path = typer.Argument(..., help="Photo for the front of the card: JPEG, PNG, WebP, GIF, BMP or TIFF."),
    card_file: Optional[Path] = typer.Option(None, "--card-file",
                                             help="JSON card: greeting and verse required; message, signature, "
                                                  "back optional. Use - to read it from stdin."),
    greeting: Optional[str] = typer.Option(None, "--greeting",
                                           help="Greeting on the front, under the photo, e.g. 'Happy Birthday!' "
                                                "(overrides the card file)."),
    verse: Optional[str] = typer.Option(None, "--verse",
                                        help="Verse on the inside right; a newline starts a new line, a blank "
                                             "line a new stanza."),
    message: Optional[str] = typer.Option(None, "--message",
                                          help="Short note on the inside left; without one that panel is left "
                                               "blank to write on."),
    signature: Optional[str] = typer.Option(None, "--signature",
                                            help="Line under the verse, e.g. 'With love, Jo'."),
    back: Optional[str] = typer.Option(None, "--back", help="Small line on the back of the card."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the sheet here instead of printing; the extension picks "
                                                "the file type: .pdf, .png, .jpg or .jpeg."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the sheet here (.pdf, .png, .jpg or "
                                                   ".jpeg) before printing it; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Greeting card: print a photo and a verse on one A4 sheet that folds into a card (A6).

    Fold the sheet in half from top to bottom with the printed side out, then in half again with the photo on the
    front. Without a card the built-in one is printed. Card flags override --card-file.
    """
    from prt import booth, card, vintage
    # Decode and check every input before a file is written or a driver exists, so a bad file name or
    # bad input never leaves a partial result or reaches the printer.
    preview_format = booth.format_for(preview) if preview else None
    copies = [(path, booth.format_for(path)) for path in save or []]
    inputs = [photo] + ([card_file] if card_file and str(card_file) != booth.STDIN else [])
    vintage.check_outputs([path for path in [preview] + [p for p, _ in copies] if path], inputs)
    image = booth.load_photo(photo)
    text = card.with_overrides(card.load_card(card_file) if card_file else card.DEFAULT_CARD, greeting=greeting,
                               verse=verse, message=message, signature=signature, back=back)
    rendered = render.card_page(image, text)
    page, cut = rendered.pages[0], rendered.cut
    if cut:
        STATE.note("warn", f"{', '.join(cut)} cut to fit the card", "shorten it, or check the card with --preview")
    if preview:
        preview_size = _save_booth_page(booth.to_bytes(page, preview_format), preview)
        STATE.note("ok", f"saved {preview}", f"greeting card · {preview_format.upper()} · "
                                             f"{preview_size / 1024:,.0f} KiB · not printed")
        STATE.note("info", card.FOLD)
    # Copies are written before connecting, so they exist even when the printer is unreachable.
    saved = []
    for path, fmt in copies:
        size = _save_booth_page(booth.to_bytes(page, fmt), path)
        STATE.note("ok", f"saved {path}", f"greeting card · {fmt.upper()} · {size / 1024:,.0f} KiB")
        saved.append({"file": str(path), "format": fmt, "bytes": size})
    extra = {"saved": saved} if save else {}
    report = {**rendered.fit(), **rendered.estimate(), "fold": card.FOLD} if STATE.json else {}
    if preview:
        if STATE.json:
            STATE.write(json.dumps({"action": "card", "file": str(preview), "bytes": preview_size, **report,
                                    **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    with _authorize(1, yes, f"Print {photo.name} as a {card.KIND} on {d.display_name}?") as ticket:
        job, = render.send_pages(d, [booth.to_jpeg(page)], [f"prt card {photo.name}"], ticket)
    STATE.note("ok", f"card queued as job {job}", d.display_name)
    STATE.note("info", card.FOLD)
    if STATE.json:
        STATE.write(json.dumps({"action": "card", "job_id": job, **report, **extra}) + "\n")


def _print_sheet(action: str, rendered: render.Rendered, label: str, preview: Optional[Path],
                 copies: list[tuple[Path, str]], question: str, job_name: str, yes: bool,
                 info: dict[str, Any], done: str | None = None,
                 precheck: Callable[[Driver], None] | None = None) -> None:
    """Save, preview or print one rendered sheet on its own paper (``prt comic``, ``prt strip``, ``prt instant``,
    ``prt colour-in``, ``prt disc``, ``prt keepalive``): the preview and the copies are written before a driver exists,
    then the sheet is sent under the print guard.

    ``precheck`` asks the printer whether it can take the sheet at all (``prt disc``: a disc tray); it runs after
    the agent-mode check, so an agent without ``--yes`` hears that first, and before the page caps and the prompt.
    """
    from prt import booth
    page, paper = rendered.pages[0], rendered.paper
    preview_format = booth.format_for(preview) if preview else None
    if rendered.cut:
        STATE.note("warn", f"{', '.join(rendered.cut)} cut to fit the {label}",
                   "shorten it, or check the print with --preview")
    if preview:
        preview_size = _save_booth_page(paper.to_bytes(page, preview_format), preview)
        STATE.note("ok", f"saved {preview}", f"{label} · {paper.label} · {preview_format.upper()} · "
                                             f"{preview_size / 1024:,.0f} KiB · not printed")
    # Copies are written before connecting, so they exist even when the printer is unreachable.
    saved = []
    for path, fmt in copies:
        size = _save_booth_page(paper.to_bytes(page, fmt), path)
        STATE.note("ok", f"saved {path}", f"{label} · {paper.label} · {fmt.upper()} · {size / 1024:,.0f} KiB")
        saved.append({"file": str(path), "format": fmt, "bytes": size})
    extra = {"saved": saved} if copies else {}
    report = {**info, "paper": paper.to_dict(), **rendered.fit(), **rendered.estimate()} if STATE.json else {}
    if preview:
        if done:
            STATE.note("info", done)
        if STATE.json:
            STATE.write(json.dumps({"action": action, "file": str(preview), "bytes": preview_size, **report,
                                    **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    if precheck:
        guard.require_consent(yes)  # agent mode: --yes first, before asking the printer anything more
        precheck(d)
    with _authorize(1, yes, f"{question} on {d.display_name}? (load {paper.loaded})") as ticket:
        job, = render.send_pages(d, rendered.jpegs(), [job_name], ticket, paper)
    STATE.note("ok", f"{label} queued as job {job}", f"{d.display_name} · {paper.loaded}")
    if done:
        STATE.note("info", done)
    if STATE.json:
        STATE.write(json.dumps({"action": action, "job_id": job, **report, **extra}) + "\n")


@app.command("comic")
def comic_cmd(
    photos: list[Path] = typer.Argument(..., help="3 to 6 photos (JPEG, PNG, WebP, GIF, BMP or TIFF), one per panel, "
                                                  "in reading order: row by row from the top, left to right."),
    comic_file: Optional[Path] = typer.Option(None, "--comic-file",
                                              help="JSON comic: panels (one per photo, each with up to 3 bubbles "
                                                   "and a caption) required; title and byline optional. Use - to "
                                                   "read it from stdin. `prt schema comic` gives its JSON Schema."),
    title: Optional[str] = typer.Option(None, "--title", help="Title above the panels (overrides the comic file); "
                                                              "'' for none."),
    byline: Optional[str] = typer.Option(None, "--byline", help="Line under the title, e.g. 'by Jo and Sam'; '' for "
                                                                "none."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the page here instead of printing; the extension picks the "
                                                "file type: .pdf, .png, .jpg or .jpeg."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the page here (.pdf, .png, .jpg or .jpeg) "
                                                   "before printing it; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Comic strip: 3 to 6 photos as the panels of a comic page, with speech bubbles, thought bubbles and captions
    (A4).

    Each panel's bubbles and caption come from --comic-file, one panel per photo; text too long for its bubble is
    cut with "…" and named in the fit report. Without a comic file a built-in comic is printed.
    """
    from prt import booth, comic, photocard, vintage
    # Check every input before a file is written or a driver exists, so a bad file name, a wrong number of
    # photos, a bad comic file or an unreadable photo never leaves a partial result or reaches the printer.
    comic.check_count(len(photos))
    copies = [(path, booth.format_for(path)) for path in save or []]
    if preview:
        booth.format_for(preview)
    inputs = list(photos) + ([comic_file] if comic_file and str(comic_file) != booth.STDIN else [])
    vintage.check_outputs([path for path in [preview] + [p for p, _ in copies] if path], inputs)
    base = comic.load_comic(comic_file) if comic_file else comic.default_comic(len(photos))
    text = comic.with_overrides(base, title=title, byline=byline)
    comic.check_panels(text, len(photos))
    images = photocard.load_photos(photos)
    rendered = render.comic_page(images, text)
    _print_sheet("comic", rendered, comic.KIND, preview, copies, f"Print {len(photos)} photos as a {comic.KIND}",
                 f"prt comic {photos[0].name}", yes, {"photos": len(photos), "title": text.title})


PREVIEW_4X6_HELP = ("Save the print here instead of printing; the extension picks the file type: .pdf, .png, .jpg "
                    "or .jpeg (4×6 in at 300 dpi).")
SAVE_4X6_HELP = "Also save a copy of the print here (.pdf, .png, .jpg or .jpeg) before printing it; repeat for more files."


@app.command("strip")
def strip_cmd(
    photos: list[Path] = typer.Argument(..., help="Four photos (JPEG, PNG, WebP, GIF, BMP or TIFF), in the order of "
                                                  "the frames from the top."),
    caption: Optional[str] = typer.Option(None, "--caption",
                                          help="Handwritten line under the frames, up to 60 characters; today's "
                                               "date when not given, '' for none."),
    mono: bool = typer.Option(False, "--mono", help="Print the photos in black and white, like a classic booth."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o", help=PREVIEW_4X6_HELP),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>", help=SAVE_4X6_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Photo-booth strip: four photos in a column, printed twice side by side on 4×6 photo paper (photo tray).

    Cut the sheet along the dashed line for two 2×6 in strips.
    """
    from prt import booth, photocard, vintage
    # Check every input before a file is written or a driver exists, so a bad file name, a wrong number of
    # photos or an unreadable photo never leaves a partial result or reaches the printer.
    photocard.check_count(len(photos))
    copies = [(path, booth.format_for(path)) for path in save or []]
    if preview:
        booth.format_for(preview)
    vintage.check_outputs([path for path in [preview] + [p for p, _ in copies] if path], photos)
    text = photocard.check_caption(photocard.today() if caption is None else caption, photocard.STRIP_CAPTION_LIMIT)
    images = photocard.load_photos(photos)
    rendered = render.strip_page(images, text, mono)
    _print_sheet("strip", rendered, photocard.STRIP_KIND, preview, copies,
                 f"Print {len(photos)} photos as a {photocard.STRIP_KIND}", f"prt strip {photos[0].name}", yes,
                 {"photos": len(photos)}, done=photocard.CUT_HINT)


@app.command("instant")
def instant_cmd(
    photo: Path = typer.Argument(..., help="Photo: JPEG, PNG, WebP, GIF, BMP or TIFF; it is cropped square."),
    caption: Optional[str] = typer.Option(None, "--caption",
                                          help="Handwritten caption under the photo, up to 80 characters; today's "
                                               "date when not given, '' to leave the space blank to write on."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o", help=PREVIEW_4X6_HELP),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>", help=SAVE_4X6_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Instant photo: a photo in a wide white frame with a handwritten caption, Polaroid-style, on 4×6 photo paper
    (photo tray)."""
    from prt import booth, photocard, vintage
    copies = [(path, booth.format_for(path)) for path in save or []]
    if preview:
        booth.format_for(preview)
    vintage.check_outputs([path for path in [preview] + [p for p, _ in copies] if path], [photo])
    text = photocard.check_caption(photocard.today() if caption is None else caption,
                                   photocard.INSTANT_CAPTION_LIMIT)
    image, = photocard.load_photos([photo])
    rendered = render.instant_page(image, text)
    _print_sheet("instant", rendered, photocard.INSTANT_KIND, preview, copies,
                 f"Print {photo.name} as an {photocard.INSTANT_KIND}", f"prt instant {photo.name}", yes, {})


@app.command("colour-in")
def colour_in_cmd(
    photo: Path = typer.Argument(..., help="Photo: JPEG, PNG, WebP, GIF, BMP or TIFF; it is printed whole, never "
                                           "cropped."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the page here instead of printing; the extension picks the "
                                                "file type: .pdf, .png, .jpg or .jpeg."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the page here (.pdf, .png, .jpg or .jpeg) "
                                                   "before printing it; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Colouring page: the photo turned into black line art on white, to colour in (A4, black ink only).

    The page is upright for a portrait photo and on its side for a landscape one.
    """
    from prt import booth, colourin, photocard, vintage
    # Check every input before a file is written or a driver exists, so a bad file name or an unreadable photo
    # never leaves a partial result or reaches the printer.
    copies = [(path, booth.format_for(path)) for path in save or []]
    if preview:
        booth.format_for(preview)
    vintage.check_outputs([path for path in [preview] + [p for p, _ in copies] if path], [photo])
    image, = photocard.load_photos([photo])
    rendered = render.colour_in_page(image)
    _print_sheet("colour-in", rendered, colourin.KIND, preview, copies, f"Print {photo.name} as a {colourin.KIND}",
                 f"prt colour-in {photo.name}", yes, {})


@app.command("recipe")
def recipe_cmd(
    source: str = typer.Argument(..., help="Link to a recipe page (http or https), or a saved file: recipe JSON, "
                                           "HTML or plain text, or - to read it from stdin."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the cards here instead of printing; the extension picks the "
                                                "file type: .pdf (every card in one file), or .png, .jpg, .jpeg (one "
                                                "file per card, numbered when there are several)."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the cards here (.pdf, .png, .jpg or .jpeg) "
                                                   "before printing them; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Print a recipe as 4×6 index cards (photo tray): the title, the ingredients and the numbered steps.

    From a web page the recipe is read from its schema.org Recipe data, or else from its Ingredients and
    Instructions headings. A recipe too long for one card runs on to more cards, up to 6.
    """
    from prt import article, booth, recipe, vintage
    # Fetch, read and lay out every card before a file is written or a driver exists, so a page that cannot be
    # fetched or holds no recipe never leaves a partial result or reaches the printer.
    preview_format = booth.format_for(preview) if preview else None
    copies = [(path, booth.format_for(path)) for path in save or []]
    text = recipe.load(source, STATE.timeout or STATE.cfg.timeout)
    rendered = render.recipe_cards(text)
    pages, paper = rendered.pages, rendered.paper
    if rendered.cut:
        STATE.note("warn", "title cut to fit the card", "shorten it in a recipe file, or check the card with --preview")
    inputs = [Path(source)] if source != booth.STDIN and not article._LINK.match(source) else []
    outputs = [(preview, preview_format)] if preview else []
    vintage.check_outputs([file for path, fmt in outputs + copies
                           for file in vintage.page_paths(path, fmt, len(pages))], inputs)
    count = f"{len(pages)} card{'s' if len(pages) != 1 else ''}"

    def write(path: Path, fmt: str) -> list[dict[str, Any]]:
        written = []
        for file, data in article.page_files(pages, path, fmt, paper):
            size = _save_booth_page(data, file)
            STATE.note("ok", f"saved {file}", f"recipe card · {paper.label} · {fmt.upper()} · {size / 1024:,.0f} KiB")
            written.append({"file": str(file), "format": fmt, "bytes": size})
        return written

    files = write(preview, preview_format) if preview else []
    # Copies are written before connecting, so they exist even when the printer is unreachable.
    saved = [entry for path, fmt in copies for entry in write(path, fmt)]
    summary = {"action": "recipe", "title": text.title, "pages": len(pages), "paper": paper.to_dict(),
               **rendered.fit(), **(rendered.estimate() if STATE.json else {})}
    extra = {"saved": saved} if save else {}
    if preview:
        STATE.note("ok", f"{count} of '{text.title}' rendered", "not printed")
        if STATE.json:
            STATE.write(json.dumps({**summary, "files": files, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    jobs: list[int] = []
    question = f"Print {count} of '{text.title}' on {d.display_name}? (load {paper.loaded})"
    with _authorize(len(pages), yes, question) as ticket:
        jobs.extend(render.send_pages(d, rendered.jpegs(), render.recipe_job_names(text.title, len(pages)), ticket,
                                      paper))
    STATE.note("ok", f"{count} queued as job{'s' if len(jobs) != 1 else ''} {', '.join(map(str, jobs))}",
               f"{d.display_name} · {paper.loaded}")
    if STATE.json:
        STATE.write(json.dumps({**summary, "job_ids": jobs, **extra}) + "\n")


@app.command("bingo")
def bingo_cmd(
    theme: Optional[str] = typer.Option(None, "--theme", "-t",
                                        help="Bundled word list: road trip (the default), beach, zoo, birthday or "
                                             "space. With --words-file it is any text and only names the game."),
    count: int = typer.Option(1, "--cards", "-n", min=1, max=30, metavar="N",
                              help="How many cards, one per player and per A4 page, each with different words "
                                   "(1 to 30)."),
    words_file: Optional[Path] = typer.Option(None, "--words-file",
                                              help="JSON words file: words (a list of at least 25) required; title "
                                                   "and free (the centre square) optional. Use - to read it from "
                                                   "stdin."),
    seed: Optional[int] = typer.Option(None, "--seed", min=0,
                                       help="Deal the same cards again: the seed a run reports (with -f json)."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the cards here instead of printing; the extension picks the "
                                                "file type: .pdf (every card in one file), or .png, .jpg, .jpeg (one "
                                                "file per card, numbered when there are several)."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the cards here (.pdf, .png, .jpg or .jpeg) "
                                                   "before printing them; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Bingo cards: a different 5×5 card for each player, one per A4 page, with a free centre square.

    The words come from a bundled theme or a words file; no two cards in a run hold the same set of words. The
    deal is random, and --seed deals the same cards again.
    """
    from prt import article, bingo, booth, vintage
    # Check every input, deal and lay out every card before a file is written or a driver exists, so a bad theme or
    # words file never leaves a partial result or reaches the printer.
    preview_format = booth.format_for(preview) if preview else None
    copies = [(path, booth.format_for(path)) for path in save or []]
    inputs = [words_file] if words_file and str(words_file) != booth.STDIN else []
    outputs = [(preview, preview_format)] if preview else []
    vintage.check_outputs([file for path, fmt in outputs + copies
                           for file in vintage.page_paths(path, fmt, count)], inputs)
    game, bundled = bingo.game(theme, bingo.load_bingo(words_file) if words_file else None)
    seed = bingo.new_seed() if seed is None else seed
    rendered = render.bingo_cards(game, bingo.deal(len(game.words), count, seed))
    pages, paper = rendered.pages, rendered.paper
    if rendered.cut:
        STATE.note("warn", f"{', '.join(rendered.cut)} cut to fit the card",
                   "shorten it, or check the cards with --preview")
    what = f"{count} bingo card{'s' if count != 1 else ''}"

    def write(path: Path, fmt: str) -> list[dict[str, Any]]:
        written = []
        for file, data in article.page_files(pages, path, fmt, paper):
            size = _save_booth_page(data, file)
            STATE.note("ok", f"saved {file}", f"bingo · {fmt.upper()} · {size / 1024:,.0f} KiB")
            written.append({"file": str(file), "format": fmt, "bytes": size})
        return written

    files = write(preview, preview_format) if preview else []
    # Copies are written before connecting, so they exist even when the printer is unreachable.
    saved = [entry for path, fmt in copies for entry in write(path, fmt)]
    summary = {"action": "bingo", "title": game.title, "theme": bundled, "pages": len(pages), "seed": seed,
               "words": len(game.words), "paper": paper.to_dict(), **rendered.fit(),
               **(rendered.estimate() if STATE.json else {})}
    extra = {"saved": saved} if save else {}
    if preview:
        STATE.note("ok", f"{what} of '{game.title}' rendered", f"seed {seed} · not printed")
        if STATE.json:
            STATE.write(json.dumps({**summary, "files": files, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    jobs: list[int] = []
    with _authorize(len(pages), yes, f"Print {what} of '{game.title}' on {d.display_name}?") as ticket:
        jobs.extend(render.send_pages(d, rendered.jpegs(), render.bingo_job_names(game.title, len(pages)), ticket,
                                      paper))
    STATE.note("ok", f"{what} queued as job{'s' if len(jobs) != 1 else ''} {', '.join(map(str, jobs))}",
               f"{d.display_name} · seed {seed}")
    if STATE.json:
        STATE.write(json.dumps({**summary, "job_ids": jobs, **extra}) + "\n")


class Difficulty(str, Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"


PUZZLE_PREVIEW_HELP = ("Save the page here instead of printing; the extension picks the file type: .pdf, .png, .jpg "
                       "or .jpeg.")
PUZZLE_SAVE_HELP = "Also save a copy of the page here (.pdf, .png, .jpg or .jpeg) before printing it; repeat for more."
PUZZLE_ANSWERS_HELP = ("Also save the solution here as JSON, the same object as the -f json line's kind, title, "
                       "seed, difficulty and solution, written before anything is printed (also with --preview). "
                       "The name must end in .json.")
PUZZLE_SEED_HELP = "Make the same puzzle again: the seed a run reports (with -f json) and prints at the foot of the page."
PUZZLE_TITLE_HELP = "Title at the top of the page, up to 60 characters."


def _puzzle_outputs(preview: Optional[Path], save: Optional[list[Path]], answers: Optional[Path],
                    inputs: list[Path]) -> list[tuple[Path, str]]:
    """Check every file a puzzle run would write before any is written: page formats, an answers name ending in
    .json, and no output on top of an input file. Returns the --save copies with their formats."""
    from prt import booth, vintage
    copies = [(path, booth.format_for(path)) for path in save or []]
    if preview:
        booth.format_for(preview)
    if answers and answers.suffix.lower() != ".json":
        raise PrinterError(f"cannot save the answers to {answers}: the name must end in .json",
                           hint="e.g. --answers answers.json", exit_code=2)
    outputs = [path for path in [preview, answers] + [p for p, _ in copies] if path]
    vintage.check_outputs(outputs, [path for path in inputs if str(path) != booth.STDIN])
    return copies


def _print_puzzle(puzzle: Any, preview: Optional[Path], copies: list[tuple[Path, str]], answers: Optional[Path],
                  yes: bool) -> None:
    """Save the answers, then preview or print the puzzle's page like any one-page sheet (``_print_sheet``); the
    -f json line carries the puzzle's kind, title, seed, difficulty and solution."""
    from prt import puzzle as puzzles
    name = puzzles.NAMES[puzzle.kind]
    rendered = render.puzzle_page(puzzle)
    info: dict[str, Any] = puzzle.answers()
    if answers:
        data = (json.dumps(puzzle.answers(), indent=2, ensure_ascii=False) + "\n").encode()
        _save_booth_page(data, answers)
        STATE.note("ok", f"saved {answers}", f"{name} solution · JSON")
        info["answers"] = str(answers)
    done = f"seed {puzzle.seed}: --seed {puzzle.seed} makes the same puzzle again" if puzzle.seed is not None else None
    _print_sheet("puzzle", rendered, name, preview, copies, f"Print the {name} '{puzzle.title}'",
                 f"prt puzzle {puzzle.kind} {puzzle.title[:60]}", yes, info, done=done)


@puzzle_app.command("sudoku")
def puzzle_sudoku(
    difficulty: Difficulty = typer.Option(Difficulty.medium, "--difficulty", "-d",
                                          help="easy (38 digits given), medium (31) or hard (about 26)."),
    seed: Optional[int] = typer.Option(None, "--seed", min=0, help=PUZZLE_SEED_HELP),
    title: Optional[str] = typer.Option(None, "--title", help=PUZZLE_TITLE_HELP),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o", help=PUZZLE_PREVIEW_HELP),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>", help=PUZZLE_SAVE_HELP),
    answers: Optional[Path] = typer.Option(None, "--answers", metavar="FILE.json", help=PUZZLE_ANSWERS_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Sudoku: a 9×9 grid with exactly one solution, rows A to I and columns 1 to 9 labelled (A4)."""
    from prt import puzzle
    copies = _puzzle_outputs(preview, save, answers, [])
    title = puzzle.check_title(title, "sudoku")
    seed = puzzle.new_seed() if seed is None else seed
    _print_puzzle(puzzle.sudoku(seed, difficulty.value, title), preview, copies, answers, yes)


@puzzle_app.command("wordsearch")
def puzzle_wordsearch(
    theme: Optional[str] = typer.Option(None, "--theme", "-t",
                                        help="Bundled word list: road trip (the default), beach, zoo, birthday or "
                                             "space. With --words-file it is any text and only names the puzzle."),
    words_file: Optional[Path] = typer.Option(None, "--words-file",
                                              help="JSON words file: words (3 to 20) required, title optional; every "
                                                   "word is hidden. Use - to read it from stdin. `prt schema "
                                                   "wordsearch` gives its JSON Schema."),
    difficulty: Difficulty = typer.Option(Difficulty.medium, "--difficulty", "-d",
                                          help="easy (10×10, forwards and down), medium (13×13, also diagonally) "
                                               "or hard (15×15, any of the eight ways, backwards too)."),
    seed: Optional[int] = typer.Option(None, "--seed", min=0, help=PUZZLE_SEED_HELP),
    title: Optional[str] = typer.Option(None, "--title", help=PUZZLE_TITLE_HELP),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o", help=PUZZLE_PREVIEW_HELP),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>", help=PUZZLE_SAVE_HELP),
    answers: Optional[Path] = typer.Option(None, "--answers", metavar="FILE.json", help=PUZZLE_ANSWERS_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Word search: words hidden in a square of letters, listed under it (A4).

    The words come from a bundled theme (a random choice of them) or a words file (all of them).
    """
    from prt import puzzle
    copies = _puzzle_outputs(preview, save, answers, [words_file] if words_file else [])
    title = puzzle.check_title(title, "wordsearch") if title is not None else None
    words = puzzle.load_words(words_file) if words_file else None
    seed = puzzle.new_seed() if seed is None else seed
    _print_puzzle(puzzle.wordsearch(seed, difficulty.value, words, theme, title), preview, copies, answers, yes)


@puzzle_app.command("crossword")
def puzzle_crossword(
    crossword_file: Optional[Path] = typer.Option(None, "--crossword-file",
                                                  help="JSON crossword: entries (2 to 20, each an answer and its "
                                                       "clue) required, title optional; prt lays out the grid. Use - "
                                                       "to read it from stdin. `prt schema crossword` gives its JSON "
                                                       "Schema. Without one a built-in space crossword is printed."),
    title: Optional[str] = typer.Option(None, "--title", help=PUZZLE_TITLE_HELP),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o", help=PUZZLE_PREVIEW_HELP),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>", help=PUZZLE_SAVE_HELP),
    answers: Optional[Path] = typer.Option(None, "--answers", metavar="FILE.json", help=PUZZLE_ANSWERS_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Crossword: the answers of a crossword file laid out to cross, numbered, with the clues under the grid (A4).

    The layout depends on the entries alone, so the same file always gives the same crossword.
    """
    from prt import puzzle
    copies = _puzzle_outputs(preview, save, answers, [crossword_file] if crossword_file else [])
    title = puzzle.check_title(title, "crossword") if title is not None else None
    file = puzzle.load_crossword(crossword_file) if crossword_file else puzzle.DEFAULT_CROSSWORD
    _print_puzzle(puzzle.crossword(file, title), preview, copies, answers, yes)


@puzzle_app.command("maze")
def puzzle_maze(
    difficulty: Difficulty = typer.Option(Difficulty.medium, "--difficulty", "-d",
                                          help="easy (10×13 squares), medium (16×21) or hard (24×32)."),
    seed: Optional[int] = typer.Option(None, "--seed", min=0, help=PUZZLE_SEED_HELP),
    title: Optional[str] = typer.Option(None, "--title", help=PUZZLE_TITLE_HELP),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o", help=PUZZLE_PREVIEW_HELP),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>", help=PUZZLE_SAVE_HELP),
    answers: Optional[Path] = typer.Option(None, "--answers", metavar="FILE.json", help=PUZZLE_ANSWERS_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Maze: one way through from Start at the top left to Finish at the bottom right (A4)."""
    from prt import puzzle
    copies = _puzzle_outputs(preview, save, answers, [])
    title = puzzle.check_title(title, "maze")
    seed = puzzle.new_seed() if seed is None else seed
    _print_puzzle(puzzle.maze(seed, difficulty.value, title), preview, copies, answers, yes)


def _require_disc_tray(d: Driver) -> None:
    """Refuse a disc label on a printer that lists its trays and has no disc tray among them (exit code 1); a
    printer that lists no trays is left to decide for itself."""
    sources = d.info().media_sources
    if sources and "disc" not in sources:
        raise PrinterError(f"{d.display_name} has no disc tray (trays: {', '.join(sources)})",
                           hint="print the label on a printer with a CD/DVD tray, or keep it with --preview",
                           kind="disc_tray_unavailable")


@app.command("disc")
def disc_cmd(
    photo: Path = typer.Argument(..., help="Photo for the label: JPEG, PNG, WebP, GIF, BMP or TIFF; it is cropped to "
                                           "fill the disc."),
    disc_file: Optional[Path] = typer.Option(None, "--disc-file",
                                             help="JSON disc label: title required; subtitle, tracks (a list) and "
                                                  "footer optional. Use - to read it from stdin."),
    title: Optional[str] = typer.Option(None, "--title", help="Title above the hub, e.g. 'Road Trip Mix' (overrides "
                                                              "the disc file)."),
    subtitle: Optional[str] = typer.Option(None, "--subtitle", help="Line under the title, e.g. 'Summer 2026'; '' for "
                                                                    "none."),
    track: Optional[list[str]] = typer.Option(None, "--track", metavar="TEXT",
                                              help="One track, e.g. '1. Open Road'; repeat it for each track, in "
                                                   "order. Replaces the disc file's tracks."),
    footer: Optional[str] = typer.Option(None, "--footer", help="Small line at the foot of the label, e.g. 'For Sam, "
                                                                "from Jo'; '' for none."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the label here instead of printing; the extension picks the "
                                                "file type: .pdf, .png, .jpg or .jpeg (a 120 mm square at 300 dpi)."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the label here (.pdf, .png, .jpg or .jpeg) "
                                                   "before printing it; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Disc label: a photo, a title and a track list on a printable CD/DVD, printed through the disc tray.

    The photo fills the printable ring (116 mm across, a 43 mm hole round the hub); the title goes above the hub,
    the tracks in two columns either side of it. Without a label the built-in mixtape is printed. Label flags
    override --disc-file.
    """
    from prt import booth, disc, vintage
    # Check every input and lay the label out before a file is written or a driver exists, so a bad file name or a
    # bad label never leaves a partial result or reaches the printer.
    copies = [(path, booth.format_for(path)) for path in save or []]
    if preview:
        booth.format_for(preview)
    inputs = [photo] + ([disc_file] if disc_file and str(disc_file) != booth.STDIN else [])
    vintage.check_outputs([path for path in [preview] + [p for p, _ in copies] if path], inputs)
    image = booth.load_photo(photo)
    text = disc.with_overrides(disc.load_disc(disc_file) if disc_file else disc.DEFAULT_DISC, title=title,
                               subtitle=subtitle, footer=footer, tracks=track)
    rendered = render.disc_label(image, text)
    _print_sheet("disc", rendered, disc.KIND, preview, copies, f"Print {photo.name} as a {disc.KIND}",
                 f"prt disc {photo.name}", yes, {"title": text.title, "tracks": len(text.tracks)},
                 precheck=_require_disc_tray)


class PostcardSide(str, Enum):
    both = "both"
    front = "front"
    back = "back"


@app.command("postcard")
def postcard_cmd(
    photo: Path = typer.Argument(..., help="Photo for the front: JPEG, PNG, WebP, GIF, BMP or TIFF; it is cropped to "
                                           "fill the card on its side."),
    postcard_file: Optional[Path] = typer.Option(None, "--postcard-file",
                                                 help="JSON postcard: message required; greeting, signature, "
                                                      "address optional. Use - to read it from stdin."),
    greeting: Optional[str] = typer.Option(None, "--greeting",
                                           help="Handwritten line across the foot of the photo, e.g. 'Greetings "
                                                "from Lisbon'; '' for none (overrides the postcard file)."),
    message: Optional[str] = typer.Option(None, "--message",
                                          help="The message on the left of the back; a newline starts a new "
                                               "paragraph."),
    signature: Optional[str] = typer.Option(None, "--signature", help="Line under the message, e.g. 'Love, Jo'."),
    address: Optional[list[str]] = typer.Option(None, "--address", metavar="LINE",
                                                help="One line of the address on the back; repeat it for each "
                                                     "line, up to 4. Without it the lines are left blank to write on."),
    side: PostcardSide = typer.Option(PostcardSide.both, "--side",
                                      help="both (default), or front or back alone: print one side at a time when "
                                           "there is no terminal to say when the card is turned over."),
    duplex: bool = typer.Option(False, "--duplex",
                                help="Print both sides in one two-sided job, through the printer's CUPS queue; for "
                                     "a printer whose duplexer takes the card."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save both sides here instead of printing; the extension picks the "
                                                "file type: .pdf (one file, front then back), or .png, .jpg, .jpeg "
                                                "(NAME-front and NAME-back)."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of both sides here (.pdf, .png, .jpg or "
                                                   ".jpeg) before printing; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Postcard: the photo on the front, the message, address lines and a stamp box on the back, on 4×6 photo
    paper (photo tray).

    Both sides print on one card: in one two-sided job when the printer can turn the card over itself (--duplex),
    else the front first, then you put the card back turned over and the back is printed. Without a postcard the
    built-in one is printed. Postcard flags override --postcard-file.
    """
    from prt import article, booth, postcard, vintage
    # Check every input and lay out both sides before a file is written or a driver exists, so a bad file name,
    # a bad postcard or a --side that cannot go with --duplex never leaves a partial result or reaches the printer.
    postcard.check_side(side.value, duplex)
    outputs = [(preview, booth.format_for(preview))] if preview else []
    copies = [(path, booth.format_for(path)) for path in save or []]
    inputs = [photo] + ([postcard_file] if postcard_file and str(postcard_file) != booth.STDIN else [])
    vintage.check_outputs([file for path, fmt in outputs + copies for file in postcard.side_paths(path, fmt)],
                          inputs)
    image = booth.load_photo(photo)
    text = postcard.with_overrides(postcard.load_postcard(postcard_file) if postcard_file
                                   else postcard.DEFAULT_POSTCARD, greeting=greeting, message=message,
                                   signature=signature, address="\n".join(address) if address else None)
    rendered = render.postcard_sides(image, text)
    paper = rendered.paper
    sides = dict(zip(postcard.SIDES, rendered.pages))
    if rendered.cut:
        STATE.note("warn", f"{', '.join(rendered.cut)} cut to fit the postcard",
                   "shorten it, or check the postcard with --preview")

    def write(path: Path, fmt: str) -> list[dict[str, Any]]:
        written = []
        files = (article.page_files(rendered.pages, path, fmt, paper) if fmt == "pdf"
                 else [(file, paper.to_bytes(page, fmt))
                       for file, page in zip(postcard.side_paths(path, fmt), rendered.pages)])
        for file, data in files:
            size = _save_booth_page(data, file)
            what = "both sides" if fmt == "pdf" else file.stem.rsplit("-", 1)[-1]
            STATE.note("ok", f"saved {file}", f"postcard {what} · {paper.label} · {fmt.upper()} · "
                                              f"{size / 1024:,.0f} KiB")
            written.append({"file": str(file), "format": fmt, "bytes": size})
        return written

    files = write(preview, outputs[0][1]) if preview else []
    # Copies are written before connecting, so they exist even when the printer is unreachable.
    saved = [entry for path, fmt in copies for entry in write(path, fmt)]
    extra = {"saved": saved} if save else {}
    printed = list(postcard.SIDES) if side == PostcardSide.both else [side.value]
    summary = {"action": "postcard", "pages": len(printed), "paper": paper.to_dict(), **rendered.fit()}
    if STATE.json:
        summary["estimate"] = guard.estimate([sides[name] for name in printed], paper)
    if preview:
        STATE.note("ok", "postcard rendered, front and back", "not printed")
        if STATE.json:
            STATE.write(json.dumps({**summary, "files": files, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    guard.require_consent(yes)  # agent mode: --yes first, so a refused flip is never the first thing an agent hears

    def two_sided() -> bool:
        return bool(d.info().duplex)

    def queue() -> str | None:
        from prt import printing
        return printing.queue_for(d)

    interactive = sys.stdin.isatty() and not guard.agent_mode()
    method, found = postcard.print_method(side.value, duplex, paper, two_sided, queue, interactive)
    name = f"prt postcard {photo.name}"
    what = {"duplex": "a two-sided postcard", "flip": "a postcard, front then back",
            "front": "the front of a postcard", "back": "the back of a postcard"}[method]
    jobs: dict[str, int | None] = {}
    with _authorize(len(printed), yes, f"Print {photo.name} as {what} on {d.display_name}? (load {paper.loaded})") \
            as ticket:
        if method == "duplex":
            job = render.send_two_sided(found or "", rendered.pages, name, ticket, paper)
            jobs = {"front": job, "back": job}
            STATE.note("ok", f"postcard queued as two-sided job {job}", f"{d.display_name} · {paper.loaded}")
        else:
            if method in ("flip", "front"):
                jobs["front"], = render.send_pages(d, [paper.to_jpeg(sides["front"])], [f"{name} (front)"],
                                                   ticket, paper)
                STATE.note("ok", f"postcard front queued as job {jobs['front']}",
                           f"{d.display_name} · {paper.loaded}")
            turned = method == "back" or (method == "flip" and typer.confirm(
                f"When the front is out: {postcard.FLIP} Print the back now?", default=True, err=True))
            if turned:
                jobs["back"], = render.send_pages(d, [paper.to_jpeg(postcard.flipped(sides["back"]))],
                                                  [f"{name} (back)"], ticket, paper)
                STATE.note("ok", f"postcard back queued as job {jobs['back']}",
                           f"{d.display_name} · {paper.loaded}")
            elif method == "flip":
                jobs["back"] = None
                STATE.note("warn", "the back was not printed", "print it later with --side back")
                summary["pages"] = 1  # the line reports what was printed: the front alone
                if STATE.json:
                    summary["estimate"] = guard.estimate([sides["front"]], paper)
    if method == "front":
        STATE.note("info", f"{postcard.FLIP} Then print the back with --side back.")
    if STATE.json:
        manual = {"flip": postcard.FLIP} if method != "duplex" else {}
        STATE.write(json.dumps({**summary, "method": method, "jobs": jobs, **manual, **extra}) + "\n")


class ArticleStyle(str, Enum):
    magazine = "magazine"
    classic_magazine = "classic-magazine"


@app.command("article")
def article_cmd(
    source: str = typer.Argument(..., help="Link to a blog post, news article or X post (http or https), or a "
                                           "saved file: article JSON, HTML or plain text, or - to read it from stdin."),
    paper: ArticleStyle = typer.Option(ArticleStyle.magazine, "--style", "-s",
                                       help="magazine (bold headline, 3 columns, colour accents) or "
                                            "classic-magazine (ivory page, centred single column, drop cap, "
                                            "ornaments)."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the pages here instead of printing; the extension picks "
                                                "the file type: .pdf (every page in one file), or .png, .jpg, "
                                                ".jpeg (one file per page, numbered when there are several)."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the pages here (.pdf, .png, .jpg or "
                                                   ".jpeg) before printing them; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Print a web article or an X post as magazine pages (A4, as many pages as the text needs).

    The title, author, site and full text are kept; navigation, ads and other page clutter are left out.
    """
    from prt import article, booth
    # Fetch, convert and lay out every page before a file is written or a driver exists, so a page that
    # cannot be fetched or converted never leaves a partial result or reaches the printer.
    preview_format = booth.format_for(preview) if preview else None
    copies = [(path, booth.format_for(path)) for path in save or []]
    text = article.load(source, STATE.timeout or STATE.cfg.timeout)
    pages = render.article_pages(text, paper.value).pages
    count = f"{len(pages)} page{'s' if len(pages) != 1 else ''}"

    def write(path: Path, fmt: str) -> list[dict[str, Any]]:
        written = []
        for file, data in article.page_files(pages, path, fmt):
            size = _save_booth_page(data, file)
            STATE.note("ok", f"saved {file}", f"{paper.value} · {fmt.upper()} · {size / 1024:,.0f} KiB")
            written.append({"file": str(file), "format": fmt, "bytes": size})
        return written

    files = write(preview, preview_format) if preview else []
    # Copies are written before connecting, so they exist even when the printer is unreachable.
    saved = [entry for path, fmt in copies for entry in write(path, fmt)]
    summary = {"action": "article", "style": paper.value, "title": text.title, "pages": len(pages),
               **(_estimate(pages) if STATE.json else {})}
    extra = {"saved": saved} if save else {}
    if preview:
        STATE.note("ok", f"{count} of '{text.title}' rendered", "not printed")
        if STATE.json:
            STATE.write(json.dumps({**summary, "files": files, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    kind = article.ARTICLE_STYLES[paper.value].kind
    jobs = []
    with _authorize(len(pages), yes, f"Print {count} of '{text.title}' as a {kind} on {d.display_name}?") as ticket:
        jobs.extend(render.send_pages(d, [booth.to_jpeg(page) for page in pages],
                                      render.article_job_names(text.title, len(pages)), ticket))
    STATE.note("ok", f"{count} queued as job{'s' if len(jobs) != 1 else ''} {', '.join(map(str, jobs))}",
               d.display_name)
    if STATE.json:
        STATE.write(json.dumps({**summary, "job_ids": jobs, **extra}) + "\n")


class GazetteEdition(str, Enum):
    photos = "photos"
    morning = "morning"


class ReceiptPeriod(str, Enum):
    day = "day"
    week = "week"


def _gazette_usage(message: str, hint: str) -> None:
    raise PrinterError(message, hint=hint, exit_code=2)


def _gazette_morning(edition_file: Path, preview: Optional[Path], copies: list[tuple[Path, str]], yes: bool) -> None:
    """The text-first morning edition of ``prt gazette``: no photos, one page from a morning JSON file."""
    from prt import morning, vintage
    outputs = [file for path, fmt in ([(preview, "pdf")] if preview else []) + copies
               for file in vintage.page_paths(path, fmt, 1)]
    vintage.check_outputs(outputs, [] if str(edition_file) == "-" else [edition_file], "morning file")
    paper = morning.load_morning(edition_file)
    rendered = render.morning_page(paper)
    info = {"edition": "morning", "photos": 0, "title": paper.title}
    _print_sheet("gazette", rendered, "morning edition", preview, copies, "Print the morning edition",
                 "prt gazette morning edition", yes, info)


@app.command("gazette")
def gazette_cmd(
    photos: Optional[list[Path]] = typer.Argument(None, help="One or more photos (JPEG, PNG, WebP, GIF, BMP or TIFF); "
                                                             "the first is the lead picture on the front page. "
                                                             "Omit them with --edition morning."),
    edition: GazetteEdition = typer.Option(GazetteEdition.photos, "--edition",
                                           help="photos (the vintage photo newspaper) or morning (the text-first "
                                                "Morning Gazette)."),
    edition_file: Optional[Path] = typer.Option(None, "--edition-file",
                                                help="Morning-edition JSON (required with --edition morning). Use - "
                                                     "to read it from stdin. `prt schema morning` gives its JSON Schema."),
    story_file: Optional[Path] = typer.Option(None, "--story-file",
                                              help="JSON story: headline and story required; kicker, caption, "
                                                   "byline optional. Use - to read it from stdin."),
    headline: Optional[str] = typer.Option(None, "--headline", help="Headline (overrides the story file)."),
    story: Optional[str] = typer.Option(None, "--story", help="Story text; a newline starts a new paragraph."),
    kicker: Optional[str] = typer.Option(None, "--kicker", help="Small label above the headline."),
    caption: Optional[str] = typer.Option(None, "--caption", help="Line under the lead photo."),
    byline: Optional[str] = typer.Option(None, "--byline", help="Author line, e.g. 'By Jo Smith'."),
    landscape: bool = typer.Option(False, "--landscape",
                                   help="Lay the pages out on A4 turned on its side: the lead photo beside the "
                                        "story, and plates three abreast. Saved pages stay landscape; each is "
                                        "turned upright only for the printer."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the pages here instead of printing; the extension picks "
                                                "the file type: .pdf (every page in one file), or .png, .jpg, "
                                                ".jpeg (one file per page, numbered when there are several)."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the pages here (.pdf, .png, .jpg or "
                                                   ".jpeg) before printing them; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Vintage gazette: print one or more photos as an old-style newspaper (A4, cream paper, one ink).

    The front page holds the story and up to three photos, each later page up to six more: none is left out.
    Without a story the gazette prints its own built-in one. Story flags override --story-file.
    """
    from prt import article, booth, vintage
    # Check every input before a file is written or a driver exists, so a bad file name, too many photos or
    # an unreadable photo never leaves a partial result or reaches the printer.
    preview_format = booth.format_for(preview) if preview else None
    copies = [(path, booth.format_for(path)) for path in save or []]
    photos = photos or []
    if edition == GazetteEdition.morning:
        if photos:
            _gazette_usage("the morning edition takes no photos", "use --edition-file morning.json without photos")
        if edition_file is None:
            _gazette_usage("the morning edition needs --edition-file morning.json",
                           "prt schema morning lists the file's shape and limits")
        if landscape or story_file or any(value is not None for value in (headline, story, kicker, caption, byline)):
            _gazette_usage("the morning edition does not take --landscape, --story-file or the story flags",
                           "its text comes from the morning JSON file")
        _gazette_morning(edition_file, preview, copies, yes)
        return
    if edition_file is not None:
        _gazette_usage("--edition-file needs --edition morning", "the photo gazette reads its story with --story-file")
    if not photos:
        _gazette_usage("the photo gazette needs at least one photo", "or use --edition morning --edition-file morning.json")
    vintage.check_count(len(photos))
    count = len(vintage.plan_pages(len(photos)))
    outputs = [(preview, preview_format)] if preview else []
    vintage.check_outputs([file for path, fmt in outputs + copies for file in vintage.page_paths(path, fmt, count)],
                          photos)
    images = vintage.load_photos(photos)
    base = booth.load_story(story_file) if story_file else vintage.DEFAULT_GAZETTE_STORY
    text = booth.with_overrides(base, headline=headline, story=story, kicker=kicker, caption=caption,
                                byline=byline)
    pages, _, cut = vintage.compose_pages(images, text, landscape=landscape)
    if cut:
        STATE.note("warn", f"{', '.join(cut)} cut to fit the front page",
                   "shorten it, or check the page with --preview")
    pictures = f"{len(photos)} photo{'s' if len(photos) != 1 else ''}"
    sheets = f"{len(pages)} page{'s' if len(pages) != 1 else ''}"

    def write(path: Path, fmt: str) -> list[dict[str, Any]]:
        written = []
        for file, data in article.page_files(pages, path, fmt):
            size = _save_booth_page(data, file)
            STATE.note("ok", f"saved {file}", f"gazette · {fmt.upper()} · {size / 1024:,.0f} KiB")
            written.append({"file": str(file), "format": fmt, "bytes": size})
        return written

    files = write(preview, preview_format) if preview else []
    # Copies are written before connecting, so they exist even when the printer is unreachable.
    saved = [entry for path, fmt in copies for entry in write(path, fmt)]
    summary = {"action": "gazette", "photos": len(photos), "pages": len(pages), **_fit_report(cut),
               **(_estimate(pages) if STATE.json else {})}
    extra = {"saved": saved} if save else {}
    if preview:
        STATE.note("ok", f"{pictures} on {sheets} rendered", "not printed")
        if STATE.json:
            STATE.write(json.dumps({**summary, "files": files, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    jobs: list[int] = []
    question = f"Print {pictures} as {sheets} of a vintage newspaper on {d.display_name}?"
    with _authorize(len(pages), yes, question) as ticket:
        for number, page in enumerate(pages, start=1):
            try:
                ticket.attempt(1)
                jobs.append(d.submit(booth.to_jpeg(vintage.for_printer(page)), "image/jpeg",
                                     PrintOptions(job_name=f"prt gazette {number}/{len(pages)}", media="a4")))
            except (PrinterError, KeyboardInterrupt):
                if jobs:  # say what is already on the printer, even under -q, so it can be cancelled
                    sys.stderr.write(f"page {number} of {len(pages)} may not have been sent; pages already queued "
                                     f"as job(s) {', '.join(map(str, jobs))}: cancel them with "
                                     "`prt jobs cancel <id>`\n")
                    if STATE.json:
                        STATE.write(json.dumps({"action": "gazette", "pages": len(pages), "job_ids": jobs,
                                                "failed_page": number}) + "\n")
                raise
    STATE.note("ok", f"{sheets} queued as job{'s' if len(jobs) != 1 else ''} {', '.join(map(str, jobs))}",
               d.display_name)
    if STATE.json:
        STATE.write(json.dumps({**summary, "job_ids": jobs, **extra}) + "\n")


@app.command("receipt")
def receipt_cmd(
    repo: Path = typer.Argument(Path("."), metavar="REPO",
                                help="Git repository to read (default: the current folder)."),
    period: ReceiptPeriod = typer.Option(ReceiptPeriod.day, "--period",
                                         help="day: commits since local midnight; week: the last seven days."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the receipt here instead of printing; the extension picks "
                                                "the file type: .pdf, .png, .jpg or .jpeg."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the receipt here (.pdf, .png, .jpg or "
                                                   ".jpeg) before printing it; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Commit receipt: print the repository's day or week of git commits as a one-page gazette."""
    from prt import booth, receipt as receipts
    preview_format = booth.format_for(preview) if preview else None
    copies = [(path, booth.format_for(path)) for path in save or []]
    del preview_format  # ``_print_sheet`` checks it again; computing it here rejects a bad name before git runs
    data = receipts.collect(repo, period.value)
    rendered = render.receipt_page(data)
    name = Path(data.repository).name or data.repository
    info = {"repository": data.repository, "period": period.value, "since": data.since.isoformat(),
            "commits": data.total, "shown": len(data.commits)}
    _print_sheet("receipt", rendered, "commit receipt", preview, copies,
                 f"Print the {period.value}'s commit receipt for {name}", f"prt receipt {name}", yes, info)


@app.command("compose")
def compose_cmd(
    page_file: Path = typer.Argument(..., metavar="PAGE",
                                     help="Page file (JSON): blocks of heading, text, image, qr, columns and rule. "
                                          "Use - to read it from stdin. `prt schema page` gives its JSON Schema."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the pages here instead of printing; the extension picks "
                                                "the file type: .pdf (every page in one file), or .png, .jpg, "
                                                ".jpeg (one file per page, numbered when there are several)."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the pages here (.pdf, .png, .jpg or "
                                                   ".jpeg) before printing them; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Compose a page from blocks (heading, text, image, QR code, columns, rule) and print it (A4).

    The blocks are laid out top to bottom and run on to more pages when one is full (up to 10); nothing is cut.
    """
    from prt import article, booth, compose, vintage
    # Read and check the page file and every image, and lay out every page, before a file is written or a
    # driver exists, so an invalid page file never leaves a partial result or reaches the printer.
    preview_format = booth.format_for(preview) if preview else None
    copies = [(path, booth.format_for(path)) for path in save or []]
    page = compose.load(page_file)
    pages = render.composed_pages(page).pages
    outputs = [(preview, preview_format)] if preview else []
    vintage.check_outputs([file for path, fmt in outputs + copies
                           for file in vintage.page_paths(path, fmt, len(pages))], page.inputs)
    count = f"{len(pages)} page{'s' if len(pages) != 1 else ''}"

    def write(path: Path, fmt: str) -> list[dict[str, Any]]:
        written = []
        for file, data in article.page_files(pages, path, fmt):
            size = _save_booth_page(data, file)
            STATE.note("ok", f"saved {file}", f"compose · {fmt.upper()} · {size / 1024:,.0f} KiB")
            written.append({"file": str(file), "format": fmt, "bytes": size})
        return written

    files = write(preview, preview_format) if preview else []
    # Copies are written before connecting, so they exist even when the printer is unreachable.
    saved = [entry for path, fmt in copies for entry in write(path, fmt)]
    summary = {"action": "compose", "title": page.title, "pages": len(pages),
               **(_estimate(pages) if STATE.json else {})}
    extra = {"saved": saved} if save else {}
    if preview:
        STATE.note("ok", f"{count} of '{page.title}' rendered", "not printed")
        if STATE.json:
            STATE.write(json.dumps({**summary, "files": files, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    jobs: list[int] = []
    with _authorize(len(pages), yes, f"Print {count} of '{page.title}' on {d.display_name}?") as ticket:
        jobs.extend(render.send_pages(d, [booth.to_jpeg(p) for p in pages],
                                      render.compose_job_names(page.title, len(pages)), ticket))
    STATE.note("ok", f"{count} queued as job{'s' if len(jobs) != 1 else ''} {', '.join(map(str, jobs))}",
               d.display_name)
    if STATE.json:
        STATE.write(json.dumps({**summary, "job_ids": jobs, **extra}) + "\n")


# --- What can be printed -------------------------------------------------------------


@app.command("styles")
def styles_cmd() -> None:
    """List every page style of booth, card, comic, strip, instant, colour-in, postcard, disc, article,
    recipe, bingo, puzzle and gazette: what it prints and the options that pick it.

    With -f json each style also lists its fields, their character limits, how many photos it takes and a
    link to a sample page. `prt schema NAME` prints the JSON Schema of a style's input file.
    """
    from prt import catalog
    items = catalog.styles()
    mode = STATE.out

    def picked(s: dict[str, Any]) -> str:
        return " ".join(["prt", s["command"], *s["args"]])

    def text() -> str:
        rows = [[t(s["name"], "accent"), t(picked(s)), t(s["kind"]), t(s["schema"] or "-")] for s in items]
        return style.box("styles", style.table_rows(["style", "command", "prints", "schema"], rows, mode), mode,
                         footer="fields and limits: prt styles -f json")

    STATE.emit(items, text, lambda: [style.plain_record(s["name"], picked(s), s["kind"], s["schema"] or "-",
                                                        s["sample"])
                                     for s in items])


def _schema_names(incomplete: str) -> list[str]:
    from prt import catalog
    return [name for name in catalog.schema_names() if name.startswith(incomplete)]


@app.command("schema")
def schema_cmd(name: str = typer.Argument(..., metavar="NAME", autocompletion=_schema_names,
                                          help="story, article, gazette, page, card, comic, recipe, postcard, "
                                               "disc, bingo, wordsearch or crossword.")) -> None:
    """Print the JSON Schema of an input file: story (prt booth --story-file), article (an article JSON file for
    prt article), gazette (prt gazette --story-file), page (a page file for prt compose), card (prt card
    --card-file), comic (prt comic --comic-file), recipe (a recipe JSON file for prt recipe), postcard (prt postcard
    --postcard-file), disc (prt disc --disc-file), bingo (prt bingo --words-file), wordsearch (prt puzzle wordsearch
    --words-file) or crossword (prt puzzle crossword --crossword-file).

    A file that passes the schema is accepted by the command. The schema is JSON whatever -f says.
    """
    from prt import catalog
    STATE.write(json.dumps(catalog.schema(name), indent=2) + "\n")


# --- Agent skills ----------------------------------------------------------------------


@skills_app.command("list")
def skills_list() -> None:
    """List the agent skills that come with prt: name, version and what each one does."""
    from prt import skills
    items = skills.bundled()
    mode = STATE.out

    def text() -> str:
        rows = [[t(s.name, "accent"), t(s.version or ""), t(s.description.split(". ")[0].rstrip("."))]
                for s in items]
        return style.box("skills", style.table_rows(["skill", "version", "what it does"], rows, mode), mode,
                         footer="install: prt skills install --claude")

    STATE.emit([s.to_dict() for s in items], text,
               lambda: [style.plain_record(s.name, s.version or "", s.description) for s in items])


@skills_app.command("install")
def skills_install(
    names: Optional[list[str]] = typer.Argument(None, metavar="[SKILL]...",
                                                help="Skills to install (default: all of them)."),
    claude: bool = typer.Option(False, "--claude", help="Install for Claude Code, in ~/.claude/skills (the default)."),
    agents: bool = typer.Option(False, "--agents",
                                help="Install for agents that read ~/.agents/skills, such as Codex."),
    project: bool = typer.Option(False, "--project",
                                 help="Install into .claude/skills or .agents/skills of the current folder instead "
                                      "of your home folder."),
    folder: Optional[Path] = typer.Option(None, "--dir", metavar="PATH",
                                          help="Install into this skills folder instead (one sub-folder per skill)."),
    force: bool = typer.Option(False, "--force",
                               help="Replace installed skill files that differ from the ones that come with prt."),
) -> None:
    """Install the agent skills that come with prt (photo-booth, article-print) where agents look for them.

    Installed files that differ from the bundled ones are never replaced without --force; then nothing is installed.
    """
    from prt import skills
    if folder is not None and (claude or agents or project):
        raise PrinterError("--dir cannot be combined with --claude, --agents or --project", exit_code=2,
                           hint="pick one: --dir PATH, or --claude / --agents (with --project for this folder)")
    if folder is not None:
        destinations = [("dir", folder.expanduser())]
    else:
        base = Path.cwd() if project else Path.home()
        chosen = [name for name, on in (("claude", claude), ("agents", agents)) if on] or ["claude"]
        destinations = [(name, skills.target_dir(name, base)) for name in chosen]
    done = skills.install(skills.select(skills.bundled(), names or []), destinations, force=force)
    for item in done:
        STATE.note("ok" if item.status != "unchanged" else "info", f"{item.skill} {item.status}", str(item.path))
    if STATE.json:
        STATE.write(json.dumps({"action": "skills_install", "skills": [item.to_dict() for item in done]}) + "\n")


@jobs_app.callback()
def jobs(ctx: typer.Context,
         all_: bool = typer.Option(False, "--all", "-a", help="Include completed jobs.")) -> None:
    """List print jobs (active only unless --all)."""
    if ctx.invoked_subcommand:
        return
    d = STATE.driver()
    d.require(Capability.JOBS)
    items = d.jobs(include_completed=all_)
    STATE.emit([to_dict(j) for j in items],
               lambda: _jobs_box(items, d, STATE.out, empty="no active jobs" if not all_ else "no jobs"),
               lambda: [_job_record(j) for j in items])



def _jobs_box(items: list[Job], d: Driver, mode: Mode, empty: str) -> str:
    if not items:
        return style.box("jobs", [style.seg(empty, "dim")], mode, footer=d.target.name)
    rows = [[str(j.id), t(j.name), style.status(JOB_LEVEL.get(j.state, "info"), j.state, mode), t(j.user or ""),
             str(j.pages if j.pages is not None else "")] for j in items]
    body = style.table_rows(["id", "name", "state", "user", "pages"], rows, mode, right={0, 4})
    return style.box(f"jobs {style.glyphs(mode)['dot']} {d.target.name}", body, mode,
                     footer=f"{len(items)} job{'s' if len(items) != 1 else ''}")


def _job_record(j: Job) -> str:
    return style.plain_record(j.id, j.name, j.state, j.user or "", "" if j.pages is None else j.pages)


def _await_job(d: Driver, job_id: int, timeout: float) -> Job:
    """Wait for one job to finish, noting the states it passes through (a failed end is the caller's error)."""
    def seen(job: Job) -> None:
        if job.state == "completed":
            STATE.note("ok", f"job {job.id} completed", d.display_name)
        elif job.state not in FINAL_JOB_STATES:
            STATE.note("info", f"job {job.id} {job.state}…", d.display_name)

    return d.wait_for_job(job_id, timeout, on_state=seen)


def _job_failure(jobs: list[Job]) -> PrinterError | None:
    """The error for jobs that ended without printing (aborted or cancelled), or None when all completed."""
    failed = [j for j in jobs if j.state != "completed"]
    if not failed:
        return None
    kind = "job_failed" if any(j.state != "canceled" for j in failed) else "job_canceled"
    what = ", ".join(f"job {j.id} {j.state}" for j in failed)
    message = what if len(jobs) == 1 else f"{len(failed)} of {len(jobs)} jobs did not print: {what}"
    hint = ("the printer stopped the job — run `prt status` to see why, then print it again"
            if kind == "job_failed" else "it was cancelled on the printer or with `prt jobs cancel`; print it again")
    return PrinterError(message, hint=hint, kind=kind)


@jobs_app.command("wait")
def jobs_wait(
    job_id: int = typer.Argument(..., help="Job id from `prt jobs` or `prt print`."),
    wait_timeout: float = typer.Option(WAIT_TIMEOUT, "--wait-timeout", min=1, metavar="SECONDS",
                                       help="Give up after this many seconds."),
) -> None:
    """Wait until a print job has finished and report how it ended; exits non-zero when it failed or was cancelled."""
    d = STATE.driver()
    d.require(Capability.JOBS)
    job = _await_job(d, job_id, wait_timeout)
    STATE.emit(to_dict(job), lambda: _jobs_box([job], d, STATE.out, empty="no such job"),
               lambda: [_job_record(job)])
    failure = _job_failure([job])
    if failure:
        raise failure


@jobs_app.command("cancel")
def jobs_cancel(job_id: int = typer.Argument(..., help="Job id from `prt jobs`.")) -> None:
    """Cancel a print job."""
    d = STATE.driver()
    d.require(Capability.JOBS)
    d.cancel_job(job_id)
    STATE.note("ok", f"job {job_id} cancelled")


class ScanFormat(str, Enum):
    pdf = "pdf"
    jpeg = "jpeg"


class ScanMode(str, Enum):
    color = "color"
    gray = "gray"
    bw = "bw"


class ScanArea(str, Enum):
    a4 = "a4"
    letter = "letter"
    full = "full"


@app.command()
def scan(
    output: Optional[str] = typer.Option(None, "--output", "-o",
                                         help="Output file ('-' for stdout) [default: scan-<time>.<ext>]."),
    fmt: ScanFormat = typer.Option(ScanFormat.pdf, "--format", "--type", "-T", help="File type: pdf or jpeg."),
    dpi: int = typer.Option(300, "--dpi", "-r", help="Resolution: 100, 200, 300, 600 or 1200."),
    mode_: ScanMode = typer.Option(ScanMode.color, "--mode", help="color, gray or bw."),
    area: ScanArea = typer.Option(ScanArea.a4, "--area", help="Scan area: a4, letter or full glass."),
) -> None:
    """Scan from the flatbed (eSCL / AirScan)."""
    d = STATE.driver()
    d.require(Capability.SCAN)
    if output == "-" and sys.stdout.isatty():
        raise PrinterError("refusing to write binary scan data to the terminal",
                           hint="redirect stdout (`> file.pdf`) or use -o FILE", exit_code=2)
    path = None if output == "-" else Path(output or time.strftime(f"scan-%Y%m%d-%H%M%S.{fmt.value}"))
    started = time.monotonic()
    show = style.progress_enabled() and not STATE.quiet

    def progress(stage: str) -> None:
        if show:
            sys.stderr.write(f"\r\x1b[2K{stage}… {time.monotonic() - started:4.1f}s" if STATE.err.styled
                             else f"\r{stage}... {time.monotonic() - started:4.1f}s   ")
            sys.stderr.flush()

    from prt.transport.escl import ScanError
    try:
        data = d.scan(ScanSettings(format=fmt.value, dpi=dpi, mode=mode_.value, area=area.value), progress)
    except ScanError as exc:
        raise PrinterError(str(exc), hint="check that the lid is closed and the scanner is idle") from exc
    finally:
        if show:
            sys.stderr.write("\r\x1b[2K" if STATE.err.styled else "\r" + " " * 40 + "\r")
    if path is None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    path.write_bytes(data)
    STATE.note("ok", f"saved {path}", f"{len(data) / 1024:,.0f} KiB · {time.monotonic() - started:.1f}s")
    if STATE.json:
        STATE.write(json.dumps({"file": str(path), "bytes": len(data)}) + "\n")


@app.command()
def tui() -> None:
    """Full-screen dashboard (keyboard driven)."""
    from prt.tui.app import run
    run(STATE.cfg, STATE.printer, STATE.timeout)


app.command("dash", hidden=True, help="Alias for tui.")(tui)


@app.command("mcp")
def mcp_cmd() -> None:
    """Run prt as an MCP server on stdin/stdout, for AI agents in any MCP client.

    Tools: status, ink, list_styles, preview_booth, preview_article (they return the rendered page images) and
    print_file, which prints only with a token from a preview or confirm: true. -p picks the default printer.
    Needs the optional MCP SDK: pip install 'printer-cli[mcp]'.
    """
    from prt import mcp_server
    mcp_server.serve(STATE.cfg, STATE.printer, STATE.timeout)


# --- Entry point ---------------------------------------------------------------------------------


def _configure_help(argv: list[str]) -> None:
    """Pass the same colour decision to Typer's rich help (btop style §4: help text)."""
    import typer.rich_utils as ru
    mode = style.decide(sys.stdout, no_color="--no-color" in argv, is_stdout=True)
    ru.FORCE_TERMINAL = mode.styled
    ru.COLOR_SYSTEM = style.TIERS[mode.tier] if mode.styled else None
    ru.STYLE_OPTION = ru.STYLE_SWITCH = ROLES["accent"]
    ru.STYLE_COMMANDS_TABLE_FIRST_COLUMN = ROLES["accent"]
    ru.STYLE_USAGE = ROLES["title"]
    ru.STYLE_OPTIONS_PANEL_BORDER = ru.STYLE_COMMANDS_PANEL_BORDER = ROLES["border"]
    ru.STYLE_ERRORS_PANEL_BORDER = ROLES["err"]
    ru.STYLE_METAVAR = ROLES["dim"] if hasattr(ru, "STYLE_METAVAR") else None


GLOBAL_WITH_VALUE = {"-p", "--printer", "-f", "--format", "--timeout", "--config"}
GLOBAL_FLAGS = {"--no-color", "-q", "--quiet", "-v", "--verbose"}


def hoist_globals(argv: list[str]) -> list[str]:
    """Allow global options anywhere (`prt ink -f json` == `prt -f json ink`)."""
    front: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest += argv[i:]
            break
        key = a.split("=", 1)[0]
        if a in GLOBAL_FLAGS:
            front.append(a)
        elif key in ("-f", "--format") and (a.split("=", 1)[1] if "=" in a else
                                             argv[i + 1] if i + 1 < len(argv) else "") not in ("text", "json"):
            rest.append(a)  # e.g. `scan --format jpeg` belongs to the command, not the global flag
        elif key in GLOBAL_WITH_VALUE and "=" in a:
            front.append(a)
        elif a in GLOBAL_WITH_VALUE and i + 1 < len(argv):
            front += [a, argv[i + 1]]
            i += 1
        else:
            rest.append(a)
        i += 1
    return front + rest


def json_requested(argv: list[str]) -> bool:
    """True when `-f json` is in effect; ``argv`` has its global options in front (see hoist_globals).

    Read from the command line rather than from STATE, so errors raised before (or while) the global
    options are parsed are JSON too.
    """
    fmt, i = Fmt.text.value, 0
    while i < len(argv):
        key, eq, value = argv[i].partition("=")
        if key in ("-f", "--format"):
            if not eq:
                i += 1
                value = argv[i] if i < len(argv) else ""
            fmt = value
        elif key in GLOBAL_WITH_VALUE:
            i += 0 if eq else 1
        elif argv[i] not in GLOBAL_FLAGS:
            break  # the command: global options end here
        i += 1
    return fmt == Fmt.json.value


def _error_json(err: PrinterError) -> str:
    return json.dumps({"error": err.to_dict()}) + "\n"


def _report(err: PrinterError, as_json: bool) -> int:
    """Print an error on stderr (a JSON object under -f json, text otherwise); returns its exit code."""
    sys.stderr.write(_error_json(err) if as_json else style.error(str(err), err.hint, STATE.err))
    return err.exit_code


def main(argv: list[str] | None = None) -> int:
    argv = hoist_globals(list(sys.argv[1:] if argv is None else argv))
    _configure_help(argv)
    as_json = json_requested(argv)
    try:
        rv = app(args=argv, prog_name="prt", standalone_mode=False)
        return rv if isinstance(rv, int) else 0
    except PrinterError as exc:
        return _report(exc, as_json)
    except typer.Exit as exc:
        return exc.exit_code
    except (KeyboardInterrupt, typer.Abort):
        if as_json:
            return _report(PrinterError("interrupted", exit_code=130, kind="interrupted"), True)
        sys.stderr.write("\n")
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:
        # Usage errors from Typer's (vendored) click: they know how to print themselves.
        if hasattr(exc, "show") and hasattr(exc, "exit_code"):
            if as_json:
                code = int(exc.exit_code)
                ctx = getattr(exc, "ctx", None)
                path = ctx.command_path if ctx is not None else "prt"
                message = exc.format_message() or f"missing command for `{path}`"  # type: ignore[attr-defined]
                return _report(PrinterError(message, f"see `{path} --help`", exit_code=code,
                                            kind=KIND_BY_EXIT_CODE.get(code, "error")), True)
            exc.show()
            return int(exc.exit_code)
        raise


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
