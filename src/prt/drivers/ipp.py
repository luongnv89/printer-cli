"""Generic driver: any IPP Everywhere / AirPrint / Mopria printer, plus eSCL scanning.

Works for most network printers made since ~2012, whatever the brand. Vendor drivers
subclass this and add what IPP cannot do (maintenance, counters).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

from prt import media
from prt.drivers.base import Driver, PrinterError, Unreachable
from prt.models import (Alert, Capability, Ink, Job, PrinterInfo, PrintOptions, ScanSettings,
                        Severity, Status, Tray)
from prt.transport import ipp
from prt.transport.ipp import Attr, IPPClient, IPPError, as_list

# printer-state-reasons keyword (without -report/-warning/-error) → friendly message
REASONS = {
    "marker-supply-low": "ink/toner low",
    "marker-supply-empty": "ink/toner empty — replace the cartridge",
    "marker-waste-almost-full": "waste ink pad almost full — service soon",
    "marker-waste-full": "waste ink pad full — printer needs service",
    "toner-low": "toner low",
    "toner-empty": "toner empty",
    "media-empty": "out of paper",
    "media-needed": "load paper",
    "media-low": "paper low",
    "media-jam": "paper jam",
    "cover-open": "cover open",
    "door-open": "door open",
    "input-tray-missing": "input tray missing",
    "output-area-full": "output tray full",
    "offline": "printer offline",
    "paused": "printer paused",
    "shutdown": "printer shutting down",
    "spool-area-full": "printer memory full",
    "interpreter-resource-unavailable": "printer busy (resources unavailable)",
    "moving-to-paused": "pausing",
    "connecting-to-device": "connecting",
    "other": "attention needed (see printer panel)",
}
IGNORED_REASONS = {"none", ""}

QUALITY = {"draft": 3, "normal": 4, "high": 5}

# Formats the printer can take directly; anything else goes through CUPS (see prt.printing).
DIRECT_FORMATS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pwg": "image/pwg-raster",
                  ".urf": "image/urf"}


def parse_reason(reason: str) -> Alert | None:
    if reason in IGNORED_REASONS:
        return None
    base, severity = reason, Severity.ERROR  # RFC 8011: no suffix means error
    for suffix, sev in (("-report", Severity.INFO), ("-warning", Severity.WARNING),
                        ("-error", Severity.ERROR)):
        if reason.endswith(suffix):
            base, severity = reason[: -len(suffix)], sev
            break
    if base in ("moving-to-paused", "connecting-to-device"):
        severity = Severity.INFO
    # Epson reports a nearly-full waste pad as "-report"; it still deserves a warning.
    if base == "marker-waste-almost-full" and severity == Severity.INFO:
        severity = Severity.WARNING
    return Alert(reason, severity, REASONS.get(base, base.replace("-", " ")))


def parse_keyvals(value: str) -> dict[str, str]:
    """Parse 'type=x;name=Sheet\\ feeder;' (printer-input-tray) honouring backslash escapes."""
    out: dict[str, str] = {}
    for part in re.split(r"(?<!\\);", value):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.replace("\\ ", " ").replace("\\;", ";").replace("\\\\", "\\")
    return out


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class IPPDriver(Driver):
    name = "ipp"
    vendor = "generic"
    capabilities = frozenset({Capability.INFO, Capability.STATUS, Capability.JOBS,
                              Capability.PRINT, Capability.SCAN, Capability.IDENTIFY})

    def __init__(self, target, timeout: float = 5.0, client: IPPClient | None = None):
        super().__init__(target, timeout)
        self.client = client or IPPClient(target.host, target.ipp_port, target.ipp_path,
                                          target.ipp_tls, timeout)
        self._attrs_cache: dict[str, Any] | None = None

    # --- transport helpers ---------------------------------------------------
    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, OSError) as exc:
            raise Unreachable(self.target, type(exc).__name__) from exc
        except IPPError as exc:
            raise PrinterError(f"printer rejected the request: {exc}") from exc

    def attributes(self, refresh: bool = True) -> dict[str, Any]:
        if refresh or self._attrs_cache is None:
            self._attrs_cache = self._call(self.client.get_printer_attributes, ["all"])
        return self._attrs_cache

    @property
    def display_name(self) -> str:
        if self._attrs_cache:
            return str(self._attrs_cache.get("printer-make-and-model") or self.target.name)
        return self.target.name

    # --- parsing (shared with vendor subclasses) ----------------------------
    def _device_id(self, a: dict[str, Any]) -> dict[str, str]:
        out = {}
        for part in str(a.get("printer-device-id") or "").split(";"):
            if ":" in part:
                k, v = part.split(":", 1)
                out[k.strip()] = v.strip()
        return out

    def _serial(self, a: dict[str, Any]) -> str | None:
        sn = self._device_id(a).get("SN")
        if not sn:
            return None
        # Epson reports SN as hex; decode it when every byte is printable, else show raw hex.
        if re.fullmatch(r"(?:[0-9A-Fa-f]{2}){4,}", sn):
            raw = bytes.fromhex(sn)
            printable = raw.rstrip(b"\x00")
            if all(32 <= b < 127 for b in printable):
                return printable.decode()
            head = bytes(b for b in raw if 32 <= b < 127).decode()
            return f"{head} (raw {sn})"
        return sn

    def parse_inks(self, a: dict[str, Any]) -> list[Ink]:
        names = as_list(a.get("marker-names"))
        levels = as_list(a.get("marker-levels"))
        colors = as_list(a.get("marker-colors"))
        lows = as_list(a.get("marker-low-levels"))
        highs = as_list(a.get("marker-high-levels"))
        kinds = as_list(a.get("marker-types"))
        inks = []
        for i, n in enumerate(names):
            lvl = _int(levels[i]) if i < len(levels) else None
            high = _int(highs[i]) if i < len(highs) else 100
            if lvl is not None and lvl < 0:
                lvl = None  # -1 unknown, -2 unknown, -3 "some remaining"
            elif lvl is not None and high and high != 100:
                lvl = round(lvl * 100 / high)
            inks.append(Ink(
                name=str(n), color=str(colors[i]) if i < len(colors) else "#888888",
                level=lvl, low=_int(lows[i]) if i < len(lows) else 15, high=100,
                kind=str(kinds[i]) if i < len(kinds) else "ink-cartridge",
            ))
        return inks

    def parse_trays(self, a: dict[str, Any]) -> list[Tray]:
        trays: list[Tray] = []
        for col in as_list(a.get("media-col-ready")):
            if not isinstance(col, dict):
                continue
            size = col.get("media-size") or {}
            trays.append(Tray(
                source=str(col.get("media-source") or "auto"),
                media=media.from_size(_int(size.get("x-dimension")), _int(size.get("y-dimension"))),
                media_type=col.get("media-type"),
            ))
        if not trays:  # fall back to media-ready keywords
            for kw in as_list(a.get("media-ready")):
                trays.append(Tray(source="auto", media=media.friendly(str(kw))))
        # Tray fill levels: only trust non-negative values (-2 = unknown, -3 = "some").
        levels = []
        for raw in as_list(a.get("printer-input-tray")):
            kv = parse_keyvals(str(raw))
            lvl, cap = _int(kv.get("level")), _int(kv.get("maxcapacity"))
            levels.append(round(lvl * 100 / cap) if lvl is not None and cap and lvl >= 0 and cap > 0 else None)
        for tray, lvl in zip(trays, levels):
            tray.level = lvl
        return trays

    def parse_status(self, a: dict[str, Any]) -> Status:
        reasons = [str(r) for r in as_list(a.get("printer-state-reasons"))]
        alerts = [al for al in (parse_reason(r) for r in reasons) if al]
        inks = self.parse_inks(a)
        # Name the specific cartridges behind a generic "supply low" reason.
        for ink in inks:
            if ink.state == "replace":
                alerts.append(Alert(f"ink-{ink.short.lower()}", Severity.ERROR,
                                    f"{ink.name.replace(' ink', '')} {ink.level}% — replace cartridge"))
            elif ink.state == "low":
                alerts.append(Alert(f"ink-{ink.short.lower()}", Severity.WARNING,
                                    f"{ink.name.replace(' ink', '')} {ink.level}% — low"))
        if any(ink.state in ("low", "replace") for ink in inks):
            alerts = [al for al in alerts if not al.code.startswith("marker-supply-low")]
        alerts.sort(key=lambda al: [Severity.ERROR, Severity.WARNING, Severity.INFO].index(al.severity))
        waste = None
        if any(r.startswith("marker-waste-full") for r in reasons):
            waste = "full"
        elif any(r.startswith("marker-waste-almost-full") for r in reasons):
            waste = "almost-full"
        elif "marker-names" in a:
            waste = "ok"
        return Status(
            state=ipp.PRINTER_STATES.get(_int(a.get("printer-state")) or 0, "unknown"),
            accepting=bool(a.get("printer-is-accepting-jobs", True)),
            inks=inks, trays=self.parse_trays(a), alerts=alerts, reasons=reasons,
            waste=waste, queued_jobs=_int(a.get("queued-job-count")) or 0,
        )

    def parse_info(self, a: dict[str, Any]) -> PrinterInfo:
        dev = self._device_id(a)
        make_model = str(a.get("printer-make-and-model") or a.get("printer-info") or self.target.name)
        sides = as_list(a.get("sides-supported"))
        return PrinterInfo(
            name=self.target.name, make_model=make_model,
            vendor=(dev.get("MFG") or make_model.split()[0]).title() if make_model else "Unknown",
            host=self.target.host, driver=self.name, model=dev.get("MDL"),
            serial=self._serial(a), firmware=a.get("printer-firmware-string-version"),
            uuid=str(a.get("printer-uuid") or "").removeprefix("urn:uuid:") or None,
            uptime_seconds=_int(a.get("printer-up-time")),
            color=a.get("color-supported"), duplex=any(str(s).startswith("two-sided") for s in sides),
            ppm=_int(a.get("pages-per-minute")), ppm_color=_int(a.get("pages-per-minute-color")),
            formats=[str(f) for f in as_list(a.get("document-format-supported"))],
            media=[m for m in (media.friendly(str(k)) for k in as_list(a.get("media-supported"))) if m],
            media_sources=[str(s) for s in as_list(a.get("media-source-supported"))],
            resolutions=[str(r) for r in as_list(a.get("printer-resolution-supported"))],
            ipp_versions=[str(v) for v in as_list(a.get("ipp-versions-supported"))],
            uris=[str(u) for u in as_list(a.get("printer-uri-supported"))],
            admin_url=a.get("printer-more-info"),
            capabilities=sorted(c.value for c in self.capabilities),
            extra={"kind": [str(k) for k in as_list(a.get("printer-kind"))],
                   "command_sets": dev.get("CMD", "").split(",") if dev.get("CMD") else []},
        )

    # --- Driver API -----------------------------------------------------------
    def info(self) -> PrinterInfo:
        return self.parse_info(self.attributes())

    def status(self) -> Status:
        return self.parse_status(self.attributes())

    def jobs(self, include_completed: bool = False) -> list[Job]:
        resp = self._call(self.client.request, ipp.OP_GET_JOBS, operation_attrs=[
            Attr("which-jobs", ipp.KEYWORD, "completed" if include_completed else "not-completed"),
            Attr("requested-attributes", ipp.KEYWORD, ["job-id", "job-name", "job-state",
                                                      "job-originating-user-name",
                                                      "job-impressions-completed",
                                                      "time-at-creation"]),
        ])
        return [self._job(j) for j in resp.groups_of(ipp.TAG_JOB)]

    @staticmethod
    def _job(j: dict[str, Any]) -> Job:
        return Job(id=_int(j.get("job-id")) or 0, name=str(j.get("job-name") or "untitled"),
                   state=ipp.JOB_STATES.get(_int(j.get("job-state")) or 0, "unknown"),
                   user=j.get("job-originating-user-name"),
                   pages=_int(j.get("job-impressions-completed")),
                   created=_int(j.get("time-at-creation")))

    def cancel_job(self, job_id: int) -> None:
        self._call(self.client.request, ipp.OP_CANCEL_JOB,
                   operation_attrs=[Attr("job-id", ipp.INTEGER, job_id),
                                    Attr("requesting-user-name", ipp.NAME, "prt")])

    def identify(self) -> None:
        self._call(self.client.request, ipp.OP_IDENTIFY_PRINTER,
                   operation_attrs=[Attr("identify-actions", ipp.KEYWORD, "flash")])

    def submit(self, data: bytes, mime: str, options: PrintOptions) -> int:
        """Send document bytes straight to the printer with IPP Print-Job."""
        job_attrs = [Attr("copies", ipp.INTEGER, max(1, options.copies))]
        if options.media or options.tray:
            col: dict[str, tuple[int, Any]] = {}
            if options.media:
                kw = media.resolve_alias(options.media)
                size = media.size_of(kw)
                if size:
                    col["media-size"] = (ipp.BEG_COLLECTION, {"x-dimension": (ipp.INTEGER, size[0]),
                                                              "y-dimension": (ipp.INTEGER, size[1])})
            if options.tray:
                col["media-source"] = (ipp.KEYWORD, options.tray)
            if col:
                job_attrs.append(Attr("media-col", ipp.BEG_COLLECTION, col))
        if options.mono:
            job_attrs.append(Attr("print-color-mode", ipp.KEYWORD, "monochrome"))
        if options.quality:
            job_attrs.append(Attr("print-quality", ipp.ENUM, QUALITY[options.quality]))
        if options.duplex:
            job_attrs.append(Attr("sides", ipp.KEYWORD, "two-sided-long-edge"))
        resp = self._call(self.client.request, ipp.OP_PRINT_JOB, operation_attrs=[
            Attr("requesting-user-name", ipp.NAME, "prt"),
            Attr("job-name", ipp.NAME, options.job_name or "prt job"),
            Attr("document-format", ipp.MIME, mime),
        ], job_attrs=job_attrs, data=data, timeout=max(self.timeout, 60))
        return _int(resp.group(ipp.TAG_JOB).get("job-id")) or 0

    def print_file(self, path: Path, options: PrintOptions) -> int:
        from prt import printing
        return printing.print_file(self, path, options)

    def scan(self, settings: ScanSettings, progress=None) -> bytes:
        from prt.transport.escl import ESCLClient
        client = ESCLClient.for_target(self.target, self.timeout)
        try:
            return client.scan(settings, progress=progress)
        except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as exc:
            raise Unreachable(self.target, f"scanner: {type(exc).__name__}") from exc
