"""Epson inkjets: everything the generic IPP driver does, plus SNMP page counters and
maintenance (nozzle check, head cleaning) through ESC/P2 "remote mode" commands on port 9100.

The remote-mode byte sequences are taken from gutenprint's escputil
(src/escputil/escputil.c: exit_packet_mode_old, start/end_remote_sequence,
do_head_clean, do_nozzle_check, do_print_cmd).
"""

from __future__ import annotations

import socket

from prt.drivers.base import PrinterError, Unreachable
from prt.drivers.ipp import IPPDriver
from prt.models import Capability, PrinterInfo, PrintOptions, Status
from prt.transport import snmp

# --- ESC/P2 remote mode (escputil.c) ------------------------------------------
EXIT_PACKET_MODE = b"\x00\x00\x00\x1b\x01@EJL 1284.4\n@EJL     \n\x1b@"
REMOTE_START = b"\x1b@\x1b(R\x08\x00\x00REMOTE1"
REMOTE_END = b"\x1b\x00\x00\x00\x1b\x00"
JOB_END = b"\x0c\x1b\x00\x1b\x00"


def remote_cmd(cmd: bytes, *args: int) -> bytes:
    """Two-letter command, 16-bit little-endian arg count, then the args."""
    return cmd + bytes([len(args) & 0xFF, (len(args) >> 8) & 0xFF]) + bytes(args)


def head_clean_payload() -> bytes:
    return EXIT_PACKET_MODE + REMOTE_START + remote_cmd(b"CH", 0, 0) + REMOTE_END + JOB_END


def nozzle_check_payload() -> bytes:
    return (EXIT_PACKET_MODE + REMOTE_START + remote_cmd(b"VI", 0, 0) + remote_cmd(b"NC", 0, 0x10)
            + remote_cmd(b"NC", 0, 0) + REMOTE_END + JOB_END)


class EpsonDriver(IPPDriver):
    name = "epson"
    vendor = "epson"
    capabilities = IPPDriver.capabilities | frozenset({
        Capability.NOZZLE_CHECK, Capability.HEAD_CLEAN, Capability.TEST_PAGE, Capability.COUNTERS,
    })

    # --- side channels (overridden by the replay driver) ----------------------
    def snmp_page_count(self) -> int | None:
        try:
            values = snmp.get(self.target.host, [snmp.PRT_LIFETIME_COUNT],
                              self.target.snmp_community, timeout=min(self.timeout, 2.0))
        except (snmp.SNMPError, OSError):
            return None
        value = values.get(snmp.PRT_LIFETIME_COUNT)
        return value if isinstance(value, int) else None

    def send_raw(self, payload: bytes) -> None:
        try:
            with socket.create_connection((self.target.host, self.target.raw_port),
                                          timeout=self.timeout) as sock:
                sock.sendall(payload)
        except OSError as exc:
            raise Unreachable(self.target, f"raw port {self.target.raw_port}: {exc}") from exc

    # --- enriched reads ----------------------------------------------------------
    def info(self) -> PrinterInfo:
        info = super().info()
        info.page_count = self.snmp_page_count()
        return info

    def status(self) -> Status:
        return super().status()

    # --- maintenance --------------------------------------------------------------
    def nozzle_check(self) -> None:
        self.send_raw(nozzle_check_payload())

    def head_clean(self) -> None:
        self.send_raw(head_clean_payload())

    def test_page(self) -> int:
        from prt.testpage import render_jpeg
        try:
            data = render_jpeg(self.display_name)
        except ImportError as exc:  # pragma: no cover - Pillow is a dependency
            raise PrinterError("test page needs Pillow", hint="uv pip install pillow") from exc
        return self.submit(data, "image/jpeg", PrintOptions(job_name="prt test page", media="a4"))
