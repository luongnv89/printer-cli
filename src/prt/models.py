"""Vendor-neutral data models shared by drivers, the CLI and the TUI."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Capability(str, Enum):
    INFO = "info"
    STATUS = "status"
    PRINT = "print"
    JOBS = "jobs"
    SCAN = "scan"
    IDENTIFY = "identify"
    NOZZLE_CHECK = "nozzle_check"
    HEAD_CLEAN = "head_clean"
    TEST_PAGE = "test_page"
    COUNTERS = "counters"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class Target:
    """Where a printer lives and how to reach each of its services."""

    name: str
    host: str
    driver: str = "auto"
    ipp_port: int = 631
    ipp_path: str = "/ipp/print"
    ipp_tls: bool = False
    scan_port: int | None = None  # None → probe 443 (TLS) then 80
    scan_tls: bool | None = None
    scan_path: str = "/eSCL"
    snmp_community: str = "public"
    raw_port: int = 9100


@dataclass
class Ink:
    name: str
    color: str  # "#RRGGBB" as reported by the printer
    level: int | None  # 0–100, None when unknown
    low: int = 15
    high: int = 100
    kind: str = "ink-cartridge"

    @property
    def short(self) -> str:
        """Two-letter label used by compact views (PB, BK, C, M, Y …)."""
        n = self.name.lower().replace(" ink", "").replace(" toner", "").strip()
        table = {
            "photo black": "PB",
            "black": "BK",
            "matte black": "MK",
            "cyan": "C",
            "magenta": "M",
            "yellow": "Y",
            "light cyan": "LC",
            "light magenta": "LM",
            "gray": "GY",
            "grey": "GY",
            "red": "R",
            "blue": "B",
            "green": "G",
        }
        return table.get(n, "".join(w[0] for w in n.split()).upper()[:2] or "?")

    @property
    def state(self) -> str:
        """ok | low | replace | unknown."""
        if self.level is None:
            return "unknown"
        if self.level <= max(3, self.low // 5):
            return "replace"
        if self.level <= self.low:
            return "low"
        return "ok"


@dataclass
class Tray:
    source: str  # main | photo | disc | manual | …
    media: str | None = None  # friendly size, e.g. "A4"
    media_type: str | None = None  # stationery | photographic | …
    level: int | None = None  # percentage, None = printer does not report it
    status: str = "ok"


@dataclass
class Alert:
    code: str
    severity: Severity
    message: str


@dataclass
class Job:
    id: int
    name: str
    state: str
    user: str | None = None
    pages: int | None = None
    created: float | None = None  # unix time


@dataclass
class Status:
    state: str  # idle | processing | stopped | offline
    accepting: bool = True
    inks: list[Ink] = field(default_factory=list)
    trays: list[Tray] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    waste: str | None = None  # ok | almost-full | full | None (unknown)
    waste_level: int | None = None  # percentage when a driver can read it
    queued_jobs: int = 0
    checked_at: float = field(default_factory=time.time)
    stale: bool = False  # True when served from the offline cache

    @property
    def worst(self) -> Severity | None:
        order = [Severity.ERROR, Severity.WARNING, Severity.INFO]
        for sev in order:
            if any(a.severity == sev for a in self.alerts):
                return sev
        return None


@dataclass
class PrinterInfo:
    name: str
    make_model: str
    vendor: str
    host: str
    driver: str
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    uuid: str | None = None
    uptime_seconds: int | None = None
    page_count: int | None = None
    color: bool | None = None
    duplex: bool | None = None
    ppm: int | None = None
    ppm_color: int | None = None
    formats: list[str] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    media_sources: list[str] = field(default_factory=list)
    resolutions: list[str] = field(default_factory=list)
    ipp_versions: list[str] = field(default_factory=list)
    uris: list[str] = field(default_factory=list)
    admin_url: str | None = None
    capabilities: list[str] = field(default_factory=list)
    scanner: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanSettings:
    format: str = "pdf"  # pdf | jpeg
    dpi: int = 300
    mode: str = "color"  # color | gray | bw
    area: str = "a4"  # a4 | letter | full


@dataclass
class PrintOptions:
    copies: int = 1
    media: str | None = None
    mono: bool = False
    quality: str | None = None  # draft | normal | high
    duplex: bool = False
    sides: str | None = None  # IPP sides keyword, e.g. two-sided-short-edge; overrides duplex (long edge)
    tray: str | None = None
    media_type: str | None = None  # e.g. photographic, stationery
    direct: bool = False
    job_name: str | None = None

    @property
    def sides_keyword(self) -> str | None:
        """The IPP ``sides`` value to send, or None for the printer's default (one-sided)."""
        return self.sides or ("two-sided-long-edge" if self.duplex else None)


def to_dict(obj: Any) -> Any:
    """JSON-ready dict for any model (enums become their values)."""

    def convert(v: Any) -> Any:
        if isinstance(v, Enum):
            return v.value
        if isinstance(v, dict):
            return {k: convert(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [convert(x) for x in v]
        return v

    data = convert(asdict(obj))
    if isinstance(obj, Ink):
        data.update(short=obj.short, state=obj.state)
    if isinstance(obj, Status):
        data["inks"] = [to_dict(i) for i in obj.inks]
        data["worst"] = obj.worst.value if obj.worst else None
    return data
