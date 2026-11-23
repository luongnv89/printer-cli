"""The driver contract. One driver per vendor/protocol family; the CLI and TUI only talk to this."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from prt.models import Capability, Job, PrinterInfo, PrintOptions, ScanSettings, Status, Target

# The kind an error gets when neither its class nor its raise site names one, by exit code.
KIND_BY_EXIT_CODE = {1: "error", 2: "usage", 3: "invalid_input"}

# Job states a job never leaves; only "completed" means the print came out.
FINAL_JOB_STATES = frozenset({"completed", "canceled", "aborted"})
JOB_POLL_INTERVAL = 2.0  # seconds between two looks at a job being waited for


class PrinterError(Exception):
    """A user-facing failure. ``hint`` tells the user how to fix it.

    ``kind`` is a stable, machine-readable name for the failure (``unreachable``, ``fetch_failed``,
    ``job_failed``, …) that scripts and agents can branch on. A raise site may name one; otherwise the
    class's ``kind`` is used, and failing that the default for the exit code (``KIND_BY_EXIT_CODE``).
    """

    kind: str | None = None

    def __init__(self, message: str, hint: str | None = None, exit_code: int = 1, kind: str | None = None):
        super().__init__(message)
        self.hint = hint
        self.exit_code = exit_code
        self.kind = kind or type(self).kind or KIND_BY_EXIT_CODE.get(exit_code, "error")

    def to_dict(self) -> dict[str, Any]:
        """The error as data: ``code`` is the process exit code."""
        return {"code": self.exit_code, "kind": self.kind, "message": str(self), "hint": self.hint}


class Unreachable(PrinterError):
    kind = "unreachable"

    def __init__(self, target: Target, detail: str = ""):
        super().__init__(
            f"cannot reach printer '{target.name}' at {target.host}" + (f" ({detail})" if detail else ""),
            hint="the printer may be asleep or off — press a button on it, or check `prt discover`",
        )


class Unsupported(PrinterError):
    kind = "unsupported"

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

    def job(self, job_id: int) -> Job | None:
        """One job, active or finished; None when the printer no longer (or never) knew it."""
        return next((j for j in self.jobs(include_completed=True) if j.id == job_id), None)

    def wait_for_job(self, job_id: int, timeout: float, interval: float | None = None,
                     on_state: Callable[[Job], None] | None = None) -> Job:
        """Poll a job until it reaches a final state (completed, canceled or aborted) and return it.

        The job is returned whatever its final state; the caller decides whether that is a failure.
        ``on_state`` is called each time the job's state changes. Raises ``PrinterError`` with kind
        ``job_not_found`` when the printer does not know the job, and ``timeout`` when it is still not
        finished after ``timeout`` seconds.
        """
        self.require(Capability.JOBS)
        interval = JOB_POLL_INTERVAL if interval is None else interval
        deadline = time.monotonic() + timeout
        last = None
        while True:
            job = self.job(job_id) if job_id > 0 else None
            if job is None:
                raise PrinterError(f"printer '{self.target.name}' has no job {job_id}",
                                   hint="see `prt jobs --all` for the jobs it knows", kind="job_not_found")
            if job.state != last:
                last = job.state
                if on_state:
                    on_state(job)
            if job.state in FINAL_JOB_STATES:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PrinterError(f"job {job_id} is still {job.state} after {timeout:g} s",
                                   hint=f"check the printer, wait again with `prt jobs wait {job_id}`, or "
                                        "allow longer with --wait-timeout", kind="timeout")
            time.sleep(min(interval, remaining))

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
