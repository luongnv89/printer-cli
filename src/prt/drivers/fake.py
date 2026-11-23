"""Replay driver: answers from a captured fixture. Used by tests, demos, and `prt -p demo`.

It runs the *real* parsing code of the IPP/Epson drivers, so tests exercise the same
paths as a live printer — only the transport is replaced.
"""

from __future__ import annotations

import copy
import io
import json
from functools import lru_cache
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

# The demo JPEG scan: a short handwritten-style letter on a sheet of A4, so the scan-a-letter workflow
# (`prt scan --format jpeg`, transcribe it, `prt gazette`) can be tried with `-p demo`.
DEMO_LETTER = (
    "Sunday, 22 November",
    "Dear Editor,",
    "Our street held its first bake sale last Saturday. Mrs. Alder's lemon cake was gone before ten, "
    "the twins ran the lemonade stand, and the brass band from the school played until it rained.",
    "Together we raised 212 euros for the new library roof. Thank you to everyone who came, baked or "
    "carried tables.",
    "Please tell your readers, so next year is even bigger.",
    "Yours sincerely,",
    "Margot Lane",
)


@lru_cache(maxsize=2)
def demo_letter_jpeg(gray: bool = False) -> bytes:
    """The demo scan as a JPEG: :data:`DEMO_LETTER` written in blue ink on a lined A4 sheet at 100 dpi."""
    from PIL import Image, ImageDraw

    from prt import fonts

    width, height, margin, line = 827, 1169, 90, 46
    page = Image.new("RGB", (width, height), (250, 248, 240))
    draw = ImageDraw.Draw(page)
    for y in range(150, height - 60, line):  # the ruled lines of the writing paper
        draw.line([(40, y), (width - 40, y)], fill=(196, 210, 228), width=1)
    font, ink = fonts.face("body-italic", 30), (28, 44, 112)
    y = 150 - 6
    for number, paragraph in enumerate(DEMO_LETTER):
        words, current = paragraph.split(), ""
        rows = []
        for word in words:
            trial = f"{current} {word}".strip()
            if current and font.getlength(trial) > width - 2 * margin:
                rows.append(current)
                current = word
            else:
                current = trial
        rows.append(current)
        indent = width - margin - font.getlength(rows[0]) if number == 0 else margin  # the date sits right
        for row in rows:
            draw.text((indent, y), row, font=font, fill=ink, anchor="ls")
            indent, y = margin, y + line
        y += line if number < 5 else 0  # a blank line after each paragraph, none inside the sign-off
    out = io.BytesIO()
    (page.convert("L") if gray else page).save(out, "JPEG", quality=85)
    return out.getvalue()


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
        # Jobs submitted to this client. Like a real printer each one goes pending → processing → its
        # final state, one step each time the active jobs are asked for; ``final_job_state`` picks how it
        # ends (9 completed, 8 aborted, 7 canceled).
        self.submitted: dict[int, dict[str, Any]] = {}
        self.final_job_state = 9

    def get_printer_attributes(self, requested=None) -> dict[str, Any]:
        return copy.deepcopy(self.fixture["printer"])

    def request(self, operation, *, operation_attrs=None, job_attrs=None, data=b"", timeout=None):
        self.requests.append((operation, operation_attrs or [], job_attrs or [], data))
        resp = ipp.Response((1, 1), 0, len(self.requests))
        if operation == ipp.OP_GET_JOBS:
            which = next((a.value for a in operation_attrs or [] if a.name == "which-jobs"), "")
            if which != "completed":
                self._advance()
            for j in [*self.fixture.get("jobs", []), *self.submitted.values()]:
                done = j.get("job-state", 9) >= 7
                if (which == "completed") == done:  # like a real printer: the two sets are disjoint
                    resp.groups.append((ipp.TAG_JOB, dict(j)))
        elif operation == ipp.OP_PRINT_JOB:
            self.next_job_id += 1
            name = next((a.value for a in operation_attrs or [] if a.name == "job-name"), "prt job")
            self.submitted[self.next_job_id] = {"job-id": self.next_job_id, "job-name": name, "job-state": 3,
                                                "job-originating-user-name": "prt"}
            resp.groups.append((ipp.TAG_JOB, {"job-id": self.next_job_id, "job-state": 3}))
        elif operation == ipp.OP_CANCEL_JOB:
            job_id = next((a.value for a in operation_attrs or [] if a.name == "job-id"), None)
            if job_id in self.submitted and self.submitted[job_id]["job-state"] < 7:
                self.submitted[job_id]["job-state"] = 7
        return resp

    def _advance(self) -> None:
        for j in self.submitted.values():
            if j["job-state"] == 3:
                j["job-state"] = 5
            elif j["job-state"] == 5:
                j["job-state"] = self.final_job_state


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
        return _PDF if settings.format == "pdf" else demo_letter_jpeg(settings.mode != "color")

    def scanner_info(self) -> dict[str, Any]:
        return dict(self.fixture.get("scanner", {}))
