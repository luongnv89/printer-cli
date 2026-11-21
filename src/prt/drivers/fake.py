"""Replay driver: answers from a captured fixture. Used by tests, demos, and `prt -p demo`.

It runs the *real* parsing code of the IPP/Epson drivers, so tests exercise the same
paths as a live printer — only the transport is replaced.
"""

from __future__ import annotations

import copy
import json
from importlib import resources
from pathlib import Path
from typing import Any

from prt.drivers.epson import EpsonDriver
from prt.models import ScanSettings, Target
from prt.transport import ipp

FIXTURE = "epson-xp6000.json"

# A tiny valid one-page PDF used as the fake scan result.
_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n")
_JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")


def load_fixture(name: str = FIXTURE) -> dict[str, Any]:
    path = Path(name)
    if path.exists():
        return json.loads(path.read_text())
    return json.loads(resources.files("prt.fixtures").joinpath(name).read_text())


class FakeIPPClient:
    """Stands in for IPPClient; records every request it receives."""

    def __init__(self, fixture: dict[str, Any]):
        self.fixture = fixture
        self.requests: list[tuple[int, list, list, bytes]] = []
        self.next_job_id = 100
        self.printer_uri = "ipp://demo:631/ipp/print"

    def get_printer_attributes(self, requested=None) -> dict[str, Any]:
        return copy.deepcopy(self.fixture["printer"])

    def request(self, operation, *, operation_attrs=None, job_attrs=None, data=b"", timeout=None):
        self.requests.append((operation, operation_attrs or [], job_attrs or [], data))
        resp = ipp.Response((1, 1), 0, len(self.requests))
        if operation == ipp.OP_GET_JOBS:
            which = next((a.value for a in operation_attrs or [] if a.name == "which-jobs"), "")
            for j in self.fixture.get("jobs", []):
                done = j.get("job-state", 9) >= 7
                if which == "completed" or not done:
                    resp.groups.append((ipp.TAG_JOB, dict(j)))
        elif operation == ipp.OP_PRINT_JOB:
            self.next_job_id += 1
            resp.groups.append((ipp.TAG_JOB, {"job-id": self.next_job_id, "job-state": 3}))
        return resp


class FakeDriver(EpsonDriver):
    name = "fake"

    def __init__(self, target: Target | None = None, timeout: float = 5.0, fixture: str = FIXTURE):
        self.fixture = load_fixture(fixture)
        target = target or Target(name="demo", host="demo", driver="fake")
        super().__init__(target, timeout, client=FakeIPPClient(self.fixture))
        self.raw_sent: list[bytes] = []

    # Replace the network side-channels with fixture data / recorders.
    def snmp_page_count(self) -> int | None:
        return self.fixture.get("snmp", {}).get("page_count")

    def send_raw(self, payload: bytes) -> None:
        self.raw_sent.append(payload)

    def scan(self, settings: ScanSettings, progress=None) -> bytes:
        for stage in ("starting", "scanning", "receiving", "done"):
            if progress:
                progress(stage)
        return _PDF if settings.format == "pdf" else _JPEG

    def scanner_info(self) -> dict[str, Any]:
        return dict(self.fixture.get("scanner", {}))
