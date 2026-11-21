"""Safety checks that run before ink-consuming maintenance."""

from __future__ import annotations

from dataclasses import dataclass, field

from prt.models import Status


@dataclass
class Preflight:
    ok: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def head_clean_preflight(status: Status) -> Preflight:
    """Head cleaning flushes ink into the waste pad. Refuse when that would hurt."""
    blockers, warnings = [], []
    if status.state in ("unknown", "") or not status.inks:
        blockers.append("printer status is unknown — refresh and try again")
    for ink in status.inks:
        if ink.level is None:
            blockers.append(f"{ink.name} level is unknown")
        elif ink.level <= ink.low:
            blockers.append(f"{ink.name} is at {ink.level}% (low level {ink.low}%) — "
                            "cleaning may empty it; replace the cartridge first")
    if status.waste in ("almost-full", "full"):
        blockers.append(f"waste ink pad is {status.waste.replace('-', ' ')} — "
                        "each cleaning adds to it; the printer may lock up when it is full")
    if status.state == "processing":
        blockers.append("printer is busy printing — wait for the job to finish")
    if status.state == "offline":
        blockers.append("printer is offline")
    if not blockers:
        warnings.append("head cleaning uses ink; run a nozzle check first to see if it is needed")
    return Preflight(ok=not blockers, blockers=blockers, warnings=warnings)


def nozzle_check_preflight(status: Status) -> Preflight:
    blockers, warnings = [], []
    if status.state == "offline":
        blockers.append("printer is offline")
    if status.state == "processing":
        blockers.append("printer is busy printing — wait for the job to finish")
    if not any(t.media for t in status.trays):
        warnings.append("no paper reported in any tray — load A4/Letter paper first")
    warnings.append("prints one page using a small amount of each ink")
    return Preflight(ok=not blockers, blockers=blockers, warnings=warnings)
