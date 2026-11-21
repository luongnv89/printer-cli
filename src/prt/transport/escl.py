"""eSCL (AirScan / Mopria Scan) client: capabilities, status, and a flatbed scan job.

Flow: POST /eSCL/ScanJobs (ScanSettings XML) → 201 + Location → GET <job>/NextDocument
until the scanner returns 404. Many scanners answer 503 while the head is still moving.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from prt.models import ScanSettings, Target

NS_SCAN = "http://schemas.hp.com/imaging/escl/2011/05/03"
NS_PWG = "http://www.pwg.org/schemas/2010/12/sm"

# Regions in 1/300 inch.
AREAS = {"a4": (2480, 3508), "letter": (2550, 3300), "a5": (1748, 2480)}
MODES = {"color": "RGB24", "gray": "Grayscale8", "bw": "BlackAndWhite1"}
FORMATS = {"pdf": "application/pdf", "jpeg": "image/jpeg", "jpg": "image/jpeg"}


class ScanError(Exception):
    pass


@dataclass
class ScannerCaps:
    make_model: str = ""
    version: str = ""
    sources: list[str] = field(default_factory=list)
    resolutions: list[int] = field(default_factory=list)
    color_modes: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    max_width: int = 2550
    max_height: int = 3510


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_capabilities(xml: str | bytes) -> ScannerCaps:
    root = ET.fromstring(xml)
    caps = ScannerCaps()
    for el in root.iter():
        name, text = _local(el.tag), (el.text or "").strip()
        if name == "MakeAndModel":
            caps.make_model = text
        elif name == "Version" and not caps.version:
            caps.version = text
        elif name == "Platen":
            caps.sources.append("platen")
        elif name == "Adf":
            caps.sources.append("adf")
        elif name == "XResolution" and text.isdigit() and int(text) not in caps.resolutions:
            caps.resolutions.append(int(text))
        elif name == "ColorMode" and text not in caps.color_modes:
            caps.color_modes.append(text)
        elif name in ("DocumentFormat", "DocumentFormatExt") and text not in caps.formats:
            caps.formats.append(text)
        elif name == "MaxWidth" and text.isdigit():
            caps.max_width = int(text)
        elif name == "MaxHeight" and text.isdigit():
            caps.max_height = int(text)
    caps.resolutions.sort()
    return caps


def parse_state(xml: str | bytes) -> str:
    for el in ET.fromstring(xml).iter():
        if _local(el.tag) == "State":
            return (el.text or "").strip()
    return "Unknown"


def build_settings(settings: ScanSettings, caps: ScannerCaps | None = None) -> str:
    if settings.format not in FORMATS:
        raise ScanError(f"unsupported scan format '{settings.format}' (use pdf or jpeg)")
    if settings.mode not in MODES:
        raise ScanError(f"unsupported colour mode '{settings.mode}' (use color, gray or bw)")
    max_w, max_h = (caps.max_width, caps.max_height) if caps else (2550, 3510)
    w, h = (max_w, max_h) if settings.area == "full" else AREAS.get(settings.area, AREAS["a4"])
    w, h = min(w, max_w), min(h, max_h)
    fmt = FORMATS[settings.format]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<scan:ScanSettings xmlns:scan="{NS_SCAN}" xmlns:pwg="{NS_PWG}">'
        "<pwg:Version>2.63</pwg:Version>"
        "<pwg:ScanRegions><pwg:ScanRegion>"
        f"<pwg:Height>{h}</pwg:Height>"
        "<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>"
        f"<pwg:Width>{w}</pwg:Width><pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>"
        "</pwg:ScanRegion></pwg:ScanRegions>"
        "<pwg:InputSource>Platen</pwg:InputSource>"
        f"<scan:ColorMode>{MODES[settings.mode]}</scan:ColorMode>"
        f"<scan:XResolution>{settings.dpi}</scan:XResolution>"
        f"<scan:YResolution>{settings.dpi}</scan:YResolution>"
        f"<pwg:DocumentFormat>{fmt}</pwg:DocumentFormat>"
        f"<scan:DocumentFormatExt>{fmt}</scan:DocumentFormatExt>"
        "</scan:ScanSettings>"
    )


class ESCLClient:
    """TLS verification is disabled for this one scanner host only: embedded devices
    use self-signed certificates and there is no CA to check them against."""

    def __init__(self, base_url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(verify=False, timeout=timeout or self.timeout, transport=self._transport)

    @classmethod
    def for_target(cls, target: Target, timeout: float = 5.0) -> "ESCLClient":
        path = "/" + target.scan_path.strip("/")
        if target.scan_port is not None:
            tls = target.scan_tls if target.scan_tls is not None else target.scan_port == 443
            return cls(f"{'https' if tls else 'http'}://{target.host}:{target.scan_port}{path}", timeout)
        # Probe HTTPS first (Epson only answers eSCL there), then plain HTTP.
        for scheme, port in (("https", 443), ("http", 80)):
            client = cls(f"{scheme}://{target.host}:{port}{path}", timeout)
            try:
                with client._client(timeout=min(timeout, 4)) as c:
                    r = c.get(f"{client.base}/ScannerCapabilities")
                if r.status_code == 200 and b"ScannerCapabilities" in r.content:
                    return client
            except httpx.HTTPError:
                continue
        raise httpx.ConnectError(f"no eSCL scanner answered on {target.host}")

    def capabilities(self) -> ScannerCaps:
        with self._client() as c:
            r = c.get(f"{self.base}/ScannerCapabilities")
        r.raise_for_status()
        return parse_capabilities(r.content)

    def state(self) -> str:
        with self._client() as c:
            r = c.get(f"{self.base}/ScannerStatus")
        r.raise_for_status()
        return parse_state(r.content)

    def _job_url(self, location: str) -> str:
        # Some scanners return an http:// Location even when contacted over https; keep our origin.
        path = urlparse(location).path or location
        origin = urlparse(self.base)
        return f"{origin.scheme}://{origin.netloc}{path}"

    def scan(self, settings: ScanSettings, progress=None, poll: float = 1.0, max_wait: float = 180) -> bytes:
        def report(stage: str) -> None:
            if progress:
                progress(stage)

        caps = None
        try:
            caps = self.capabilities()
        except httpx.HTTPError:
            pass
        if caps and caps.resolutions and settings.dpi not in caps.resolutions:
            raise ScanError(f"{settings.dpi} dpi not supported; choose one of "
                            + ", ".join(map(str, caps.resolutions)))
        body = build_settings(settings, caps)
        report("starting")
        with self._client(timeout=max(self.timeout, 30)) as c:
            r = c.post(f"{self.base}/ScanJobs", content=body, headers={"Content-Type": "text/xml"})
            if r.status_code == 503:
                raise ScanError("scanner is busy — try again in a moment")
            if r.status_code not in (200, 201) or "location" not in r.headers:
                raise ScanError(f"scanner refused the job (HTTP {r.status_code})")
            job = self._job_url(r.headers["location"])
            report("scanning")
            start = time.monotonic()
            pages: list[bytes] = []
            while True:
                r = c.get(f"{job}/NextDocument", timeout=max(self.timeout, 120))
                if r.status_code == 200:
                    pages.append(r.content)
                    report("receiving")
                    if settings.format == "pdf":
                        break  # platen PDF = one document
                    continue
                if r.status_code == 404 and pages:
                    break
                if r.status_code in (503, 404) and time.monotonic() - start < max_wait:
                    time.sleep(poll)
                    continue
                raise ScanError(f"scan failed (HTTP {r.status_code} from NextDocument)")
            try:
                c.delete(job)
            except httpx.HTTPError:
                pass
        report("done")
        return pages[0] if len(pages) == 1 else b"".join(pages)
