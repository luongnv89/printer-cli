"""Find printers and scanners on the LAN with mDNS/DNS-SD (Bonjour)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from prt.models import Target

SERVICES = ["_ipp._tcp.local.", "_ipps._tcp.local.", "_uscan._tcp.local.", "_uscans._tcp.local."]


@dataclass
class Discovered:
    name: str  # Bonjour instance name, e.g. "EPSON XP-6000 Series"
    host: str  # e.g. "EPSON721655.local"
    addresses: list[str] = field(default_factory=list)
    make_model: str | None = None
    uuid: str | None = None
    ipp: bool = False
    ipp_port: int = 631
    ipp_path: str = "/ipp/print"
    ipp_tls: bool = False
    scanner: bool = False
    scan_port: int | None = None
    scan_tls: bool | None = None
    scan_path: str = "/eSCL"
    admin_url: str | None = None

    @property
    def services(self) -> list[str]:
        out = []
        if self.ipp:
            out.append("ipps" if self.ipp_tls else "ipp")
        if self.scanner:
            out.append("escl")
        return out

    def suggested_name(self) -> str:
        base = (self.make_model or self.name).lower()
        for junk in (" series", "epson ", "hp ", "canon ", "brother "):
            base = base.replace(junk, " ")
        return "-".join(base.split())[:24] or "printer"

    def to_target(self, name: str | None = None) -> Target:
        return Target(name=name or self.suggested_name(), host=self.host, ipp_port=self.ipp_port,
                      ipp_path=self.ipp_path, ipp_tls=self.ipp_tls, scan_port=self.scan_port,
                      scan_tls=self.scan_tls, scan_path=self.scan_path)


def _txt(info) -> dict[str, str]:
    out = {}
    for k, v in (info.properties or {}).items():
        key = k.decode(errors="replace") if isinstance(k, bytes) else str(k)
        val = v.decode(errors="replace") if isinstance(v, bytes) else ("" if v is None else str(v))
        out[key.lower()] = val
    return out


def merge(found: dict[str, Discovered], service: str, instance: str, host: str,
          port: int, addresses: list[str], txt: dict[str, str]) -> None:
    """Fold one DNS-SD record into the per-device table (keyed by UUID, else host)."""
    key = (txt.get("uuid") or host).lower()
    d = found.get(key)
    if d is None:
        d = found[key] = Discovered(name=instance, host=host)
    d.addresses = sorted(set(d.addresses) | set(addresses))
    d.make_model = d.make_model or txt.get("ty") or None
    d.uuid = d.uuid or txt.get("uuid") or None
    d.admin_url = d.admin_url or txt.get("adminurl") or None
    if service.startswith("_ipp"):
        tls = service.startswith("_ipps")
        if not d.ipp or (d.ipp_tls and not tls):  # prefer plain IPP: fewer TLS quirks on embedded devices
            d.ipp, d.ipp_port, d.ipp_tls = True, port, tls
            d.ipp_path = "/" + (txt.get("rp") or "ipp/print").strip("/")
    elif service.startswith("_uscan"):
        tls = service.startswith("_uscans") or port == 443
        d.scanner, d.scan_port, d.scan_tls = True, port, tls
        d.scan_path = "/" + (txt.get("rs") or "eSCL").strip("/")


def discover(timeout: float = 3.0) -> list[Discovered]:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: dict[str, Discovered] = {}
    pending: list[tuple[str, str]] = []

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            pending.append((type_, name))

        def update_service(self, zc, type_, name):
            pending.append((type_, name))

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        browsers = [ServiceBrowser(zc, s, Listener()) for s in SERVICES]
        deadline = time.monotonic() + timeout
        seen: set[tuple[str, str]] = set()
        while time.monotonic() < deadline or pending:
            if not pending:
                time.sleep(0.1)
                continue
            type_, name = pending.pop(0)
            if (type_, name) in seen:
                continue
            seen.add((type_, name))
            info = zc.get_service_info(type_, name, timeout=1500)
            if not info or not info.server:
                continue
            instance = name.removesuffix("." + type_)
            merge(found, type_, instance, info.server.rstrip("."), info.port or 0,
                  info.parsed_addresses(), _txt(info))
        for b in browsers:
            b.cancel()
    finally:
        zc.close()
    return sorted(found.values(), key=lambda d: d.name.lower())
