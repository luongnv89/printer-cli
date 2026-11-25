"""prt TUI — the full-screen dashboard (`prt tui`)."""

from __future__ import annotations

import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Input, Label, RichLog, Select, Static, Switch,
                             TabbedContent, TabPane)

from prt import cache, guard
from prt.config import Config
from prt.drivers.base import Driver, PrinterError, Unreachable
from prt.drivers.registry import make_driver
from prt.maintenance import Preflight, head_clean_preflight, nozzle_check_preflight
from prt.models import Capability, PrinterInfo, PrintOptions, ScanSettings, Status, Target
from prt.tui.widgets import (AMBER, GREEN, RED, Panel, render_alerts, render_paper, render_system,
                             render_tanks, render_topbar)

REFRESH_SECONDS = 10


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no dialog that lists what will happen and anything that blocks it."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str, preflight: Preflight | None = None, confirm: str = "Confirm",
                 danger: bool = False):
        super().__init__()
        self.question, self.preflight, self.confirm_label, self.danger = question, preflight, confirm, danger

    def compose(self) -> ComposeResult:
        details = Text()
        if self.preflight:
            for b in self.preflight.blockers:
                details.append("✗ ", style=f"bold {RED}")
                details.append(b + "\n")
            for w in self.preflight.warnings:
                details.append("▲ ", style=f"bold {AMBER}")
                details.append(w + "\n")
        with Vertical(id="dialog"):
            yield Label(self.question, id="question")
            yield Static(details, id="details")
            with Horizontal(classes="actions"):
                yield Button("Cancel", id="cancel")
                label = self.confirm_label
                if self.preflight and not self.preflight.ok:
                    label = f"{label} anyway"
                yield Button(label, id="confirm", classes="danger" if self.danger or
                             (self.preflight and not self.preflight.ok) else "")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()  # the safe choice has focus

    @on(Button.Pressed, "#confirm")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _no(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PrtApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "prt"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("d", "tab('dashboard')", "Dashboard", show=False),
        Binding("m", "tab('maintain')", "Maintain", show=False),
        Binding("p", "tab('print')", "Print"),
        Binding("s", "tab('scan')", "Scan"),
        Binding("j", "tab('jobs')", "Jobs"),
        Binding("n", "nozzle", "Nozzle check"),
        Binding("c", "clean", "Clean"),
        Binding("i", "identify", "Identify"),
        Binding("q", "quit", "Quit"),
        *[Binding(str(k), f"switch({k - 1})", show=False) for k in range(1, 10)],
    ]

    def __init__(self, targets: list[Target], timeout: float = 5.0, notice: str | None = None,
                 cfg: Config | None = None):
        super().__init__()
        self.targets = targets
        self.timeout = timeout
        self.cfg = cfg  # its page caps hold here too (prt.guard); the TUI's own dialogs are the consent
        self.active = 0
        self.drivers: dict[str, Driver] = {}
        self.status: Status | None = None
        self.info: PrinterInfo | None = None
        self.busy = False
        self.notice = notice

    # --- layout -----------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                with Vertical(id="dash"):
                    yield Panel("Ink", id="ink")
                    yield Panel("System", id="system")
                    yield Panel("Paper", id="paper")
                    yield Panel("Alerts", id="alerts")
            with TabPane("Maintain", id="maintain"):
                yield Static("Run a nozzle check first. Clean the head only if lines in the pattern are "
                             "broken — cleaning uses ink and fills the waste pad.", classes="hint")
                with Horizontal(classes="actions"):
                    yield Button("Nozzle check", id="btn-nozzle")
                    yield Button("Head cleaning", id="btn-clean")
                    yield Button("Test page", id="btn-test")
                    yield Button("Identify", id="btn-identify")
                yield RichLog(id="log", markup=False, highlight=False, wrap=True)
            with TabPane("Print", id="print"):
                yield Static("JPEG and PNG go straight to the printer; PDF and other files go through "
                             "this computer's print queue.", classes="hint")
                with Vertical(classes="field"):
                    yield Label("File")
                    yield Input(placeholder="path to a file, e.g. ~/Pictures/photo.jpg", id="print-file")
                with Horizontal(classes="field-row"):
                    with Vertical(classes="field"):
                        yield Label("Copies")
                        yield Input("1", id="print-copies", type="integer")
                    with Vertical(classes="field"):
                        yield Label("Paper")
                        yield Select([("A4", "a4"), ("Letter", "letter"), ("4×6 photo", "4x6"), ("5×7 photo", "5x7"),
                                      ("8×10", "8x10"), ("A5", "a5")],
                                     value="a4", id="print-media", allow_blank=False)
                    with Vertical(classes="field"):
                        yield Label("Tray")
                        yield Select([("Automatic", "auto"), ("Main tray", "main"), ("Photo tray", "photo")],
                                     value="auto", id="print-tray", allow_blank=False)
                    with Vertical(classes="field"):
                        yield Label("Quality")
                        yield Select([("Draft", "draft"), ("Normal", "normal"), ("High", "high")], value="normal",
                                     id="print-quality", allow_blank=False)
                    with Vertical(classes="field"):
                        yield Label("Black & white")
                        yield Switch(False, id="print-mono")
                with Horizontal(classes="actions"):
                    yield Button("Print", id="btn-print")
                yield Static("", id="print-result")
            with TabPane("Scan", id="scan"):
                yield Static("Place the document face down on the glass and close the lid.", classes="hint")
                with Horizontal(classes="field-row"):
                    with Vertical(classes="field"):
                        yield Label("Type")
                        yield Select([("PDF", "pdf"), ("JPEG", "jpeg")], value="pdf", id="scan-format",
                                     allow_blank=False)
                    with Vertical(classes="field"):
                        yield Label("Resolution")
                        yield Select([(f"{d} dpi", d) for d in (100, 200, 300, 600, 1200)], value=300,
                                     id="scan-dpi", allow_blank=False)
                    with Vertical(classes="field"):
                        yield Label("Colour")
                        yield Select([("Colour", "color"), ("Grey", "gray"), ("Black & white", "bw")],
                                     value="color", id="scan-mode", allow_blank=False)
                    with Vertical(classes="field"):
                        yield Label("Area")
                        yield Select([("A4", "a4"), ("Letter", "letter"), ("Whole glass", "full")],
                                     value="a4", id="scan-area", allow_blank=False)
                with Vertical(classes="field"):
                    yield Label("Save to folder")
                    yield Input(str(Path.cwd()), id="scan-dir")
                with Horizontal(classes="actions"):
                    yield Button("Scan", id="btn-scan")
                yield Static("", id="scan-result")
            with TabPane("Jobs", id="jobs"):
                with Horizontal(classes="actions"):
                    yield Button("Refresh jobs", id="btn-jobs")
                    yield Button("Cancel selected", id="btn-cancel-job", classes="danger")
                yield DataTable(id="jobs-table", cursor_type="row", zebra_stripes=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        table.add_columns("ID", "Name", "State", "User", "Pages")
        self.paint()
        self.action_refresh()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)
        if self.notice:
            self.notify(self.notice, severity="warning", timeout=8)

    def on_resize(self, event) -> None:
        self.query_one("#dash").set_class(event.size.width < 100, "narrow")

    # --- state ------------------------------------------------------------------------
    @property
    def target(self) -> Target:
        return self.targets[self.active]

    def driver(self) -> Driver:
        t = self.target
        if t.name not in self.drivers:
            self.drivers[t.name] = make_driver(t, self.timeout)
        return self.drivers[t.name]

    def paint(self) -> None:
        labels = [(t.name, True) for t in self.targets]
        model = self.info.make_model if self.info else self.target.host
        self.query_one("#topbar", Static).update(
            render_topbar(self.target.name, model, self.status, labels, self.active, self.busy))
        self.query_one("#ink", Panel).update(
            render_tanks(self.status.inks if self.status else [], waste=self.status.waste if self.status else None))
        self.query_one("#system", Panel).update(render_system(self.info))
        self.query_one("#paper", Panel).update(render_paper(self.status))
        self.query_one("#alerts", Panel).update(render_alerts(self.status))
        caps = self.drivers.get(self.target.name)
        for btn, cap in (("#btn-nozzle", Capability.NOZZLE_CHECK), ("#btn-clean", Capability.HEAD_CLEAN),
                         ("#btn-test", Capability.TEST_PAGE), ("#btn-identify", Capability.IDENTIFY),
                         ("#btn-print", Capability.PRINT), ("#btn-scan", Capability.SCAN)):
            self.query_one(btn, Button).disabled = bool(caps) and not caps.supports(cap)

    def log_line(self, level: str, message: str) -> None:
        sym, colour = {"ok": ("●", GREEN), "warn": ("▲", AMBER), "err": ("✗", RED)}.get(level, ("◆", "#FFFFFF"))
        line = Text(time.strftime("%H:%M:%S  "))
        line.append(f"{sym} ", style=f"bold {colour}")
        line.append(message)
        self.query_one("#log", RichLog).write(line)

    # --- background work (drivers are synchronous) ------------------------------------
    @work(thread=True, exclusive=True, group="refresh")
    def action_refresh(self) -> None:
        self.busy = True
        self.call_from_thread(self.paint)
        name = self.target.name
        try:
            d = self.driver()
            st = d.status()
            cache.save(name, st)
            info = self.info if self.info and self.info.name == name else d.info()
            self.call_from_thread(self._apply, name, st, info, None)
        except Unreachable as exc:
            st = cache.load(name)
            self.call_from_thread(self._apply, name, st, self.info if self.info and self.info.name == name
                                  else None, str(exc))
        except PrinterError as exc:
            self.call_from_thread(self._apply, name, None, None, str(exc))

    def _apply(self, name: str, st: Status | None, info: PrinterInfo | None, error: str | None) -> None:
        if name != self.target.name:
            return  # the user switched printers meanwhile
        self.busy = False
        self.status, self.info = st, info
        if error and (st is None or st.stale):
            if not getattr(self, "_warned", None) == (name, error):
                self.notify(error + (" — showing last known status" if st else ""), severity="warning",
                            timeout=6)
                self._warned = (name, error)
        self.paint()

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        if tab == "jobs":
            self.load_jobs()

    def action_switch(self, index: int) -> None:
        if index < len(self.targets) and index != self.active:
            self.active = index
            self.status, self.info = cache.load(self.target.name), None
            self.paint()
            self.action_refresh()

    # --- maintenance --------------------------------------------------------------------
    def action_nozzle(self) -> None:
        self._maintenance("nozzle")

    def action_clean(self) -> None:
        self._maintenance("clean")

    def action_identify(self) -> None:
        self._run_action("identify")

    @on(Button.Pressed, "#btn-nozzle")
    def _b_nozzle(self) -> None:
        self._maintenance("nozzle")

    @on(Button.Pressed, "#btn-clean")
    def _b_clean(self) -> None:
        self._maintenance("clean")

    @on(Button.Pressed, "#btn-test")
    def _b_test(self) -> None:
        self._maintenance("test")

    @on(Button.Pressed, "#btn-identify")
    def _b_identify(self) -> None:
        self._run_action("identify")

    def _maintenance(self, kind: str) -> None:
        st = self.status or Status(state="unknown")
        if st.stale:
            st = Status(state="offline", inks=st.inks, trays=st.trays, waste=st.waste)
        if kind == "clean":
            pf, q, label = head_clean_preflight(st), "Clean the print head?", "Clean"
        elif kind == "nozzle":
            pf, q, label = nozzle_check_preflight(st), "Print a nozzle check pattern?", "Print pattern"
        else:
            pf, q, label = Preflight(ok=True, warnings=["prints one A4 page in colour"]), \
                "Print the colour test page?", "Print"
        if st.state == "offline" and kind != "clean":
            pf.ok = False
        self.query_one(TabbedContent).active = "maintain"

        def done(confirmed: bool | None) -> None:
            if confirmed:
                if not pf.ok:
                    self.log_line("warn", f"{label} confirmed despite: {'; '.join(pf.blockers)}")
                self._run_action(kind)
            else:
                self.log_line("info", "cancelled — nothing was sent")

        self.push_screen(ConfirmScreen(q, pf, label, danger=kind == "clean"), done)

    @work(thread=True, group="action")
    def _run_action(self, kind: str) -> None:
        messages = {"nozzle": "nozzle check sent — broken lines mean clogged nozzles",
                    "clean": "head cleaning started — wait until the printer is quiet, then run a nozzle check",
                    "test": "test page queued", "identify": "the printer should be flashing now"}
        try:
            d = self.driver()
            if kind in ("nozzle", "test"):
                with guard.authorize(self.cfg, 1, yes=True) as ticket:
                    ticket.attempt(1)
                    if kind == "nozzle":
                        d.nozzle_check()
                    else:
                        messages["test"] = f"test page queued as job {d.test_page()}"
            elif kind == "clean":
                d.head_clean()
            elif kind == "identify":
                d.identify()
            self.call_from_thread(self.log_line, "ok", messages[kind])
            self.call_from_thread(self.notify, messages[kind])
        except PrinterError as exc:
            self.call_from_thread(self.log_line, "err", str(exc) + (f" — {exc.hint}" if exc.hint else ""))
            self.call_from_thread(self.notify, str(exc), severity="error")

    # --- print ---------------------------------------------------------------------------
    @on(Button.Pressed, "#btn-print")
    def _b_print(self) -> None:
        raw = self.query_one("#print-file", Input).value.strip()
        result = self.query_one("#print-result", Static)
        if not raw:
            result.update(Text("✗ choose a file to print first", style=f"bold {RED}"))
            self.query_one("#print-file", Input).focus()
            return
        path = Path(raw).expanduser()
        if not path.is_file():
            result.update(Text(f"✗ file not found: {path}", style=f"bold {RED}"))
            return
        try:
            copies = max(1, min(99, int(self.query_one("#print-copies", Input).value or "1")))
        except ValueError:
            copies = 1
        tray = self.query_one("#print-tray", Select).value
        opts = PrintOptions(copies=copies, media=str(self.query_one("#print-media", Select).value),
                            tray=None if tray == "auto" else str(tray),
                            quality=str(self.query_one("#print-quality", Select).value),
                            mono=self.query_one("#print-mono", Switch).value)
        result.update(Text(f"sending {path.name}…"))
        self._send_print(path, opts)

    @work(thread=True, group="action")
    def _send_print(self, path: Path, opts: PrintOptions) -> None:
        result = self.query_one("#print-result", Static)
        try:
            pages = guard.job_pages([path], opts.copies)
            with guard.authorize(self.cfg, pages, yes=True) as ticket:
                job = self.driver().print_file(path, opts, before_send=lambda: ticket.attempt(pages or 0))
            msg = Text("● ", style=f"bold {GREEN}")
            msg.append(f"{path.name} sent" + (f" as job {job}" if job else ""))
            self.call_from_thread(result.update, msg)
        except PrinterError as exc:
            self.call_from_thread(result.update, Text(f"✗ {exc}" + (f" — {exc.hint}" if exc.hint else ""),
                                                      style=f"bold {RED}"))

    # --- scan -----------------------------------------------------------------------------
    @on(Button.Pressed, "#btn-scan")
    def _b_scan(self) -> None:
        settings = ScanSettings(format=str(self.query_one("#scan-format", Select).value),
                                dpi=int(self.query_one("#scan-dpi", Select).value),
                                mode=str(self.query_one("#scan-mode", Select).value),
                                area=str(self.query_one("#scan-area", Select).value))
        folder = Path(self.query_one("#scan-dir", Input).value.strip() or ".").expanduser()
        if not folder.is_dir():
            self.query_one("#scan-result", Static).update(Text(f"✗ folder not found: {folder}", style=f"bold {RED}"))
            return
        self.query_one("#btn-scan", Button).disabled = True
        self._scan(settings, folder)

    @work(thread=True, group="action")
    def _scan(self, settings: ScanSettings, folder: Path) -> None:
        result = self.query_one("#scan-result", Static)
        start = time.monotonic()

        def progress(stage: str) -> None:
            self.call_from_thread(result.update, Text(f"{stage}… {time.monotonic() - start:.0f}s"))

        try:
            data = self.driver().scan(settings, progress)
            path = folder / time.strftime(f"scan-%Y%m%d-%H%M%S.{settings.format}")
            path.write_bytes(data)
            msg = Text("● ", style=f"bold {GREEN}")
            msg.append(f"saved {path}  ({len(data) / 1024:,.0f} KiB, {time.monotonic() - start:.0f}s)")
            self.call_from_thread(result.update, msg)
        except Exception as exc:  # scan errors come from several layers; show them all the same way
            hint = getattr(exc, "hint", None)
            self.call_from_thread(result.update, Text(f"✗ {exc}" + (f" — {hint}" if hint else ""),
                                                      style=f"bold {RED}"))
        finally:
            self.call_from_thread(setattr, self.query_one("#btn-scan", Button), "disabled", False)

    # --- jobs -------------------------------------------------------------------------------
    @on(Button.Pressed, "#btn-jobs")
    def _b_jobs(self) -> None:
        self.load_jobs()

    @on(TabbedContent.TabActivated)
    def _tab_changed(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "jobs":
            self.load_jobs()

    @work(thread=True, exclusive=True, group="jobs")
    def load_jobs(self) -> None:
        try:
            jobs = self.driver().jobs(include_completed=True)
        except PrinterError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
            return

        def fill() -> None:
            table = self.query_one("#jobs-table", DataTable)
            table.clear()
            for j in sorted(jobs, key=lambda x: -x.id):
                colour = {"completed": GREEN, "canceled": AMBER, "aborted": RED}.get(j.state, "#FFFFFF")
                table.add_row(str(j.id), j.name, Text(j.state, style=colour), j.user or "",
                              "" if j.pages is None else str(j.pages), key=str(j.id))
            if not jobs:
                table.add_row("—", "no jobs", "", "", "")

        self.call_from_thread(fill)

    @on(Button.Pressed, "#btn-cancel-job")
    def _b_cancel(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        if not table.row_count:
            return
        row = table.get_row_at(table.cursor_row)
        if not str(row[0]).isdigit():
            return
        job_id = int(row[0])

        def done(ok: bool | None) -> None:
            if ok:
                self._cancel(job_id)

        self.push_screen(ConfirmScreen(f"Cancel job {job_id}?", None, "Cancel job", danger=True), done)

    @work(thread=True, group="jobs")
    def _cancel(self, job_id: int) -> None:
        try:
            self.driver().cancel_job(job_id)
            self.call_from_thread(self.notify, f"job {job_id} cancelled")
        except PrinterError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
        self.load_jobs()


def build_targets(cfg: Config, selector: str | None) -> tuple[list[Target], str | None]:
    """Saved printers (default first); falls back to discovery, then to the demo printer."""
    targets = list(cfg.printers.values())
    notice = None
    if selector and selector not in cfg.printers:
        targets.insert(0, cfg.resolve(selector))
    elif cfg.default in cfg.printers:
        targets.sort(key=lambda t: t.name != (selector or cfg.default))
    if selector in cfg.printers:
        targets.sort(key=lambda t: t.name != selector)
    if not targets:
        try:
            targets = [cfg.resolve(None)]
        except PrinterError as exc:
            targets = [Target(name="demo", host="demo", driver="fake")]
            notice = f"{exc} — showing the demo printer. Add yours with `prt printers add`."
    return targets, notice


def run(cfg: Config, selector: str | None = None, timeout: float | None = None) -> None:
    targets, notice = build_targets(cfg, selector)
    PrtApp(targets, timeout or cfg.timeout, notice, cfg=cfg).run()
