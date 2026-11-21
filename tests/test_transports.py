from pathlib import Path

import httpx
import pytest

from prt.discovery import Discovered, merge
from prt.models import ScanSettings
from prt.printing import match_queue
from prt.transport import escl, snmp

CAPS = (Path(__file__).parent / "fixtures" / "escl-caps.xml").read_bytes()


def test_escl_capabilities():
    caps = escl.parse_capabilities(CAPS)
    assert caps.make_model == "EPSON XP-6000 Series"
    assert caps.sources == ["platen"]
    assert caps.resolutions == [100, 200, 300, 600, 1200]
    assert "RGB24" in caps.color_modes and "application/pdf" in caps.formats
    assert (caps.max_width, caps.max_height) == (2550, 3510)


def test_escl_settings_xml():
    xml = escl.build_settings(ScanSettings(format="jpeg", dpi=600, mode="gray", area="a4"))
    assert "<scan:ColorMode>Grayscale8</scan:ColorMode>" in xml
    assert "<scan:XResolution>600</scan:XResolution>" in xml
    assert "<pwg:Width>2480</pwg:Width>" in xml and "image/jpeg" in xml
    with pytest.raises(escl.ScanError):
        escl.build_settings(ScanSettings(format="tiff"))


def test_escl_scan_flow():
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("ScannerCapabilities"):
            return httpx.Response(200, content=CAPS)
        if req.method == "POST":
            return httpx.Response(201, headers={"Location": "http://10.0.0.5/eSCL/ScanJobs/abc"})
        if req.url.path.endswith("NextDocument"):
            n = sum(1 for c in calls if c[1].endswith("NextDocument"))
            return httpx.Response(503) if n == 1 else httpx.Response(200, content=b"%PDF-1.4 scan")
        return httpx.Response(200)

    c = escl.ESCLClient("https://scanner:443/eSCL", transport=httpx.MockTransport(handler))
    stages = []
    data = c.scan(ScanSettings(), progress=stages.append, poll=0)
    assert data == b"%PDF-1.4 scan"
    assert ("GET", "/eSCL/ScanJobs/abc/NextDocument") in calls
    assert stages[0] == "starting" and stages[-1] == "done"


def test_escl_rejects_unsupported_dpi():
    def handler(req):
        return httpx.Response(200, content=CAPS)
    c = escl.ESCLClient("http://s/eSCL", transport=httpx.MockTransport(handler))
    with pytest.raises(escl.ScanError, match="dpi"):
        c.scan(ScanSettings(dpi=150))


def test_snmp_round_trip():
    pkt = snmp.encode_get([snmp.PRT_LIFETIME_COUNT], "public", 42)
    assert pkt[0] == 0x30 and b"public" in pkt
    assert snmp.decode_oid(snmp.encode_oid("1.3.6.1.4.1.1248.1.2")[2:]) == "1.3.6.1.4.1.1248.1.2"
    # Hand-built GetResponse: counter32 1538 for the page-count OID
    oid = snmp.encode_oid(snmp.PRT_LIFETIME_COUNT)
    vb = snmp._tlv(0x30, oid + snmp._tlv(snmp.COUNTER32, (1538).to_bytes(2, "big")))
    pdu = snmp._tlv(snmp.GET_RESPONSE, snmp._int(42) + snmp._int(0) + snmp._int(0) + snmp._tlv(0x30, vb))
    msg = snmp._tlv(0x30, snmp._int(0) + snmp._tlv(4, b"public") + pdu)
    assert snmp.decode_response(msg) == {snmp.PRT_LIFETIME_COUNT: 1538}


def test_discovery_merge_prefers_plain_ipp_and_maps_scanner():
    found = {}
    txt = {"uuid": "abc", "ty": "EPSON XP-6000 Series", "rp": "ipp/print"}
    merge(found, "_ipps._tcp.local.", "EPSON XP-6000 Series", "EPSON721655.local", 631, ["192.168.1.70"], txt)
    merge(found, "_ipp._tcp.local.", "EPSON XP-6000 Series", "EPSON721655.local", 631, ["192.168.1.70"], txt)
    merge(found, "_uscan._tcp.local.", "EPSON XP-6000 Series", "EPSON721655.local", 443, [],
          {"uuid": "abc", "rs": "eSCL"})
    (d,) = found.values()
    assert d.ipp and not d.ipp_tls and d.scanner and d.scan_tls and d.scan_port == 443
    assert d.services == ["ipp", "escl"]
    assert d.suggested_name() == "xp-6000"
    assert d.to_target().scan_port == 443


def test_cups_queue_matching():
    out = ("device for EPSON_XP_6000_Series: dnssd://EPSON%20XP-6000%20Series._ipps._tcp.local./?uuid=cfe92100-67c4\n"
           "device for Pantum: ipp://pantum.local/ipp/print\n")
    assert match_queue(out, "cfe92100-67c4", "x") == "EPSON_XP_6000_Series"
    assert match_queue(out, None, "pantum.local") == "Pantum"
    assert match_queue(out, None, "nothing") is None
