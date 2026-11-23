"""prt — command-line control for network printers."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import typer
from rich.text import Text

from prt import __version__, cache, style
from prt.config import Config
from prt.drivers.base import Driver, PrinterError, Unreachable
from prt.drivers.registry import driver_names, make_driver
from prt.maintenance import head_clean_preflight, nozzle_check_preflight
from prt.models import Capability, PrintOptions, ScanSettings, Severity, Status, to_dict
from prt.style import ROLES, Mode, t

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
jobs_app = typer.Typer(help="List or cancel print jobs.", invoke_without_command=True)
app.add_typer(printers_app, name="printers")
app.add_typer(maintain_app, name="maintain")
app.add_typer(jobs_app, name="jobs")


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


def _finish_stale() -> None:
    if getattr(STATE, "_stale_exit", False):
        raise typer.Exit(1)


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


@app.command()
def discover(timeout: float = typer.Option(3.0, "--wait", help="Seconds to listen for mDNS answers.")) -> None:
    """Find printers and scanners on the local network (mDNS / Bonjour)."""
    from prt.discovery import discover as run
    if STATE.err.styled and not STATE.quiet:
        sys.stderr.write(style.render(STATE.err, Text(f"listening for printers for {timeout:g}s…",
                                                      style=ROLES["dim"])) + "\n")
    found = run(timeout)
    if not found:
        STATE.note("warn", "no printers answered",
                   "a sleeping printer does not reply to discovery — wake it (press a button) and retry")
    mode = STATE.out
    saved = {p.host.lower(): n for n, p in STATE.cfg.printers.items()}

    def text() -> str:
        if not found:
            return style.box("discover", [style.seg("no printers answered", "dim")], mode,
                             footer="0 found")
        rows = [[f.make_model or f.name, f.host, f.addresses[0] if f.addresses else "",
                 " ".join(f.services), saved.get(f.host.lower(), "")] for f in found]
        body = style.table_rows(["model", "host", "address", "services", "saved as"], rows, mode)
        hint = Text(f"save one: prt printers add {found[0].suggested_name()} {found[0].host} --default",
                    style=ROLES["dim"])
        return style.box("discover", [*body, None, hint], mode, footer=f"{len(found)} found")

    STATE.emit([f.__dict__ | {"services": f.services} for f in found], text,
               lambda: [style.plain_record(f.make_model or f.name, f.host,
                                           f.addresses[0] if f.addresses else "", ",".join(f.services))
                        for f in found])


# --- printers ---------------------------------------------------------------------------


@printers_app.command("list")
def printers_list(check: bool = typer.Option(False, "--check", help="Ping each printer over IPP.")) -> None:
    """Saved printers, their driver and which one is the default."""
    cfg = STATE.cfg
    rows_data = []
    for name, tgt in cfg.printers.items():
        reach = None
        if check:
            try:
                make_driver(tgt, min(cfg.timeout, 3)).status()
                reach = "online"
            except PrinterError:
                reach = "offline"
        rows_data.append({"name": name, "host": tgt.host, "driver": tgt.driver,
                          "default": name == cfg.default, "reachable": reach})
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
    if yes:
        return
    if not sys.stdin.isatty():
        raise PrinterError("confirmation needed but stdin is not a terminal", hint="re-run with --yes",
                           exit_code=2)
    if not typer.confirm(question, default=False, err=True):
        STATE.note("info", "cancelled — nothing was sent")
        raise typer.Exit(1)


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
        raise PrinterError("nozzle check not sent", hint="fix the issues above and retry")
    _confirm(f"Print a nozzle check on {d.display_name}?", yes)
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
                           hint="replace low cartridges first, or pass --force if you understand the risk")
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
    _confirm(f"Print a colour test page on {d.display_name}?", yes)
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


# --- print / jobs / scan -------------------------------------------------------------------


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
) -> None:
    """Print one or more files."""
    d = STATE.driver()
    d.require(Capability.PRINT)
    results = []
    for f in files:
        opts = PrintOptions(copies=copies, media=media, mono=mono, quality=quality.value if quality else None,
                            duplex=duplex, tray=tray, direct=direct, job_name=f.name)
        job = d.print_file(f, opts)
        results.append({"file": str(f), "job_id": job})
        STATE.note("ok", f"{f.name} → job {job}" if job else f"{f.name} sent", d.display_name)
    if STATE.json:
        STATE.write(json.dumps(results) + "\n")


class BoothStyle(str, Enum):
    metro = "metro"
    daily = "daily"
    wizard = "wizard"
    wanted = "wanted"


@app.command("booth")
def booth_cmd(
    photo: Path = typer.Argument(..., help="Photo of a person: JPEG, PNG, WebP, GIF, BMP or TIFF."),
    paper: BoothStyle = typer.Option(BoothStyle.metro, "--style", "-s",
                                     help="metro (colour tabloid, 2 columns), daily (black-and-white "
                                          "broadsheet, 3 columns), wizard (ornate trading card) or wanted "
                                          "(weathered pirate poster)."),
    story_file: Optional[Path] = typer.Option(None, "--story-file",
                                              help="JSON story: headline and story required; kicker, caption, "
                                                   "byline optional."),
    headline: Optional[str] = typer.Option(None, "--headline",
                                           help="Headline; the name on wizard and wanted (overrides the story "
                                                "file)."),
    story: Optional[str] = typer.Option(None, "--story",
                                        help="Story text, or the lore / description on wizard and wanted; a "
                                             "newline starts a new paragraph."),
    kicker: Optional[str] = typer.Option(None, "--kicker",
                                         help="Small label above the headline; the card type on wizard, the "
                                              "'Dead or alive' line on wanted."),
    caption: Optional[str] = typer.Option(None, "--caption",
                                          help="Line under the photo; the subtitle on wizard, the reward line on "
                                               "wanted."),
    byline: Optional[str] = typer.Option(None, "--byline",
                                         help="Author line, e.g. 'By Jo Smith'; the footer on wizard and wanted."),
    preview: Optional[Path] = typer.Option(None, "--preview", "--output", "-o",
                                           help="Save the page here instead of printing; the extension picks "
                                                "the file type: .pdf, .png, .jpg or .jpeg."),
    save: Optional[list[Path]] = typer.Option(None, "--save", metavar="<path>",
                                              help="Also save a copy of the page here (.pdf, .png, .jpg or "
                                                   ".jpeg) before printing it; repeat for more files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Photo booth: print a photo as a newspaper front page, a wizard card or a wanted poster (A4).

    Without a story each style uses its own built-in one. Story flags override --story-file.
    """
    from prt import booth
    # Decode and check every input before a file is written or a driver exists, so a bad file name or
    # bad input never leaves a partial result or reaches the printer.
    preview_format = booth.format_for(preview) if preview else None
    copies = [(path, booth.format_for(path)) for path in save or []]
    image = booth.load_photo(photo)
    base = booth.load_story(story_file) if story_file else booth.default_story(paper.value)
    text = booth.with_overrides(base, headline=headline, story=story, kicker=kicker, caption=caption,
                                byline=byline)
    page, cut = booth.compose(image, text, paper.value)
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
    if preview:
        if STATE.json:
            STATE.write(json.dumps({"action": "booth", "style": paper.value, "file": str(preview),
                                    "bytes": preview_size, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    _confirm(f"Print {photo.name} as a {booth.KINDS[paper.value]} on {d.display_name}?", yes)
    job = d.submit(booth.to_jpeg(page), "image/jpeg",
                   PrintOptions(job_name=f"prt booth {photo.name}", media="a4"))
    STATE.note("ok", f"booth page queued as job {job}", d.display_name)
    if STATE.json:
        STATE.write(json.dumps({"action": "booth", "style": paper.value, "job_id": job, **extra}) + "\n")


def _save_booth_page(data: bytes, path: Path) -> int:
    try:
        path.write_bytes(data)
    except OSError as exc:
        raise PrinterError(f"cannot write {path}: {exc.strerror}",
                           hint="check that the folder exists and is writable") from exc
    return len(data)


class ArticleStyle(str, Enum):
    magazine = "magazine"
    classic_magazine = "classic-magazine"


@app.command("article")
def article_cmd(
    source: str = typer.Argument(..., help="Link to a blog post, news article or X post (http or https), or a "
                                           "saved file: article JSON, HTML or plain text."),
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
    pages = article.compose_pages(text, paper.value)
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
    summary = {"action": "article", "style": paper.value, "title": text.title, "pages": len(pages)}
    extra = {"saved": saved} if save else {}
    if preview:
        STATE.note("ok", f"{count} of '{text.title}' rendered", "not printed")
        if STATE.json:
            STATE.write(json.dumps({**summary, "files": files, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    kind = article.ARTICLE_STYLES[paper.value].kind
    _confirm(f"Print {count} of '{text.title}' as a {kind} on {d.display_name}?", yes)
    jobs = []
    for number, page in enumerate(pages, start=1):
        jobs.append(d.submit(booth.to_jpeg(page), "image/jpeg",
                             PrintOptions(job_name=f"prt article {number}/{len(pages)} {text.title[:60]}",
                                          media="a4")))
    STATE.note("ok", f"{count} queued as job{'s' if len(jobs) != 1 else ''} {', '.join(map(str, jobs))}",
               d.display_name)
    if STATE.json:
        STATE.write(json.dumps({**summary, "job_ids": jobs, **extra}) + "\n")


@app.command("gazette")
def gazette_cmd(
    photos: list[Path] = typer.Argument(..., help="One or more photos (JPEG, PNG, WebP, GIF, BMP or TIFF); the "
                                                  "first is the lead picture on the front page."),
    story_file: Optional[Path] = typer.Option(None, "--story-file",
                                              help="JSON story: headline and story required; kicker, caption, "
                                                   "byline optional."),
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
    summary = {"action": "gazette", "photos": len(photos), "pages": len(pages)}
    extra = {"saved": saved} if save else {}
    if preview:
        STATE.note("ok", f"{pictures} on {sheets} rendered", "not printed")
        if STATE.json:
            STATE.write(json.dumps({**summary, "files": files, **extra}) + "\n")
        return
    d = STATE.driver()
    d.require(Capability.PRINT)
    _confirm(f"Print {pictures} as {sheets} of a vintage newspaper on {d.display_name}?", yes)
    jobs: list[int] = []
    for number, page in enumerate(pages, start=1):
        try:
            jobs.append(d.submit(booth.to_jpeg(vintage.for_printer(page)), "image/jpeg",
                                 PrintOptions(job_name=f"prt gazette {number}/{len(pages)}", media="a4")))
        except (PrinterError, KeyboardInterrupt):
            if jobs:  # say what is already on the printer, even under -q, so it can be cancelled
                sys.stderr.write(f"page {number} of {len(pages)} may not have been sent; pages already queued as "
                                 f"job(s) {', '.join(map(str, jobs))}: cancel them with `prt jobs cancel <id>`\n")
                if STATE.json:
                    STATE.write(json.dumps({"action": "gazette", "pages": len(pages), "job_ids": jobs,
                                            "failed_page": number}) + "\n")
            raise
    STATE.note("ok", f"{sheets} queued as job{'s' if len(jobs) != 1 else ''} {', '.join(map(str, jobs))}",
               d.display_name)
    if STATE.json:
        STATE.write(json.dumps({**summary, "job_ids": jobs, **extra}) + "\n")


@jobs_app.callback()
def jobs(ctx: typer.Context,
         all_: bool = typer.Option(False, "--all", "-a", help="Include completed jobs.")) -> None:
    """List print jobs (active only unless --all)."""
    if ctx.invoked_subcommand:
        return
    d = STATE.driver()
    d.require(Capability.JOBS)
    items = d.jobs(include_completed=all_)
    mode = STATE.out

    def text() -> str:
        if not items:
            return style.box("jobs", [style.seg("no active jobs" if not all_ else "no jobs", "dim")],
                             mode, footer=d.target.name)
        rows = []
        for j in items:
            lvl = {"completed": "ok", "processing": "info", "pending": "info", "canceled": "warn",
                   "aborted": "err"}.get(j.state, "info")
            rows.append([str(j.id), t(j.name), style.status(lvl, j.state, mode), t(j.user or ""),
                         str(j.pages if j.pages is not None else "")])
        body = style.table_rows(["id", "name", "state", "user", "pages"], rows, mode, right={0, 4})
        return style.box(f"jobs {style.glyphs(mode)['dot']} {d.target.name}", body, mode,
                         footer=f"{len(items)} job{'s' if len(items) != 1 else ''}")

    STATE.emit([to_dict(j) for j in items], text,
               lambda: [style.plain_record(j.id, j.name, j.state, j.user or "",
                                           "" if j.pages is None else j.pages) for j in items])


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


def main(argv: list[str] | None = None) -> int:
    argv = hoist_globals(list(sys.argv[1:] if argv is None else argv))
    _configure_help(argv)
    try:
        rv = app(args=argv, prog_name="prt", standalone_mode=False)
        return rv if isinstance(rv, int) else 0
    except PrinterError as exc:
        sys.stderr.write(style.error(str(exc), exc.hint, STATE.err))
        return exc.exit_code
    except typer.Exit as exc:
        return exc.exit_code
    except (KeyboardInterrupt, typer.Abort):
        sys.stderr.write("\n")
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:
        # Usage errors from Typer's (vendored) click: they know how to print themselves.
        if hasattr(exc, "show") and hasattr(exc, "exit_code"):
            exc.show()
            return int(exc.exit_code)
        raise


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
