"""The driver contract. One driver per vendor/protocol family; the CLI and TUI only talk to this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from prt.models import Capability, Job, PrinterInfo, PrintOptions, ScanSettings, Status, Target


class PrinterError(Exception):
    """A user-facing failure. ``hint`` tells the user how to fix it."""

    def __init__(self, message: str, hint: str | None = None, exit_code: int = 1):
        super().__init__(message)
        self.hint = hint
        self.exit_code = exit_code


class Unreachable(PrinterError):
    def __init__(self, target: Target, detail: str = ""):
        super().__init__(
            f"cannot reach printer '{target.name}' at {target.host}" + (f" ({detail})" if detail else ""),
            hint="the printer may be asleep or off — press a button on it, or check `prt discover`",
        )


class Unsupported(PrinterError):
    def __init__(self, driver: "Driver", capability: Capability):
        super().__init__(
            f"{driver.display_name} does not support {capability.value.replace('_', ' ')} "
            f"(capability: {capability.value}, driver: {driver.name})",
            hint="run `prt overview` to see what this printer supports",
        )


class Driver(ABC):
    """Base class. Subclasses declare ``capabilities`` and override the matching methods."""

    name: str = "base"
    vendor: str = "generic"
    capabilities: frozenset[Capability] = frozenset()

    def __init__(self, target: Target, timeout: float = 5.0):
        self.target = target
        self.timeout = timeout

    @property
    def display_name(self) -> str:
        return self.target.name

    def supports(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def require(self, cap: Capability) -> None:
        if not self.supports(cap):
            raise Unsupported(self, cap)

    # --- read --------------------------------------------------------------
    @abstractmethod
    def info(self) -> PrinterInfo: ...

    @abstractmethod
    def status(self) -> Status: ...

    def jobs(self, include_completed: bool = False) -> list[Job]:
        raise Unsupported(self, Capability.JOBS)

    # --- act ---------------------------------------------------------------
    def cancel_job(self, job_id: int) -> None:
        raise Unsupported(self, Capability.JOBS)

    def print_file(self, path: Path, options: PrintOptions) -> int:
        """Submit a file; returns the job id."""
        raise Unsupported(self, Capability.PRINT)

    def scan(self, settings: ScanSettings, progress=None) -> bytes:
        raise Unsupported(self, Capability.SCAN)

    def identify(self) -> None:
        raise Unsupported(self, Capability.IDENTIFY)

    def nozzle_check(self) -> None:
        raise Unsupported(self, Capability.NOZZLE_CHECK)

    def head_clean(self) -> None:
        raise Unsupported(self, Capability.HEAD_CLEAN)

    def test_page(self) -> int:
        raise Unsupported(self, Capability.TEST_PAGE)
