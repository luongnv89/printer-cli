import struct

import pytest

from prt.transport import ipp
from prt.transport.ipp import Attr


def _response(groups, status=0):
    """Build a response with the encoder's attribute writer (round-trip)."""
    out = bytearray(struct.pack(">BBHI", 2, 0, status, 7))
    for tag, attrs in groups:
        out.append(tag)
        for a in attrs:
            out += ipp._encode_attr(a.name, a.tag, a.value)
    out.append(ipp.TAG_END)
    return bytes(out)


def test_encode_request_header_and_required_attrs():
    data = ipp.encode_request(ipp.OP_GET_PRINTER_ATTRIBUTES, "ipp://h:631/ipp/print", request_id=9)
    assert data[:8] == struct.pack(">BBHI", 1, 1, 0x000B, 9)
    assert data[8] == ipp.TAG_OPERATION
    assert b"attributes-charset" in data and b"utf-8" in data
    assert b"printer-uri" in data and b"ipp://h:631/ipp/print" in data
    assert data.endswith(bytes([ipp.TAG_END]))


def test_round_trip_scalars_sets_and_collections():
    raw = _response([(ipp.TAG_OPERATION, [Attr("attributes-charset", ipp.CHARSET, "utf-8")]),
                     (ipp.TAG_PRINTER, [
                         Attr("printer-state", ipp.ENUM, 3),
                         Attr("marker-levels", ipp.INTEGER, [92, 1, 1, 12, 89]),
                         Attr("marker-names", ipp.NAME, ["Cyan ink", "Black ink"]),
                         Attr("color-supported", ipp.BOOLEAN, True),
                         Attr("copies-supported", ipp.RANGE, (1, 99)),
                         Attr("printer-resolution-default", ipp.RESOLUTION, ipp.Resolution(360, 360)),
                         Attr("media-col-default", ipp.BEG_COLLECTION, {
                             "media-size": (ipp.BEG_COLLECTION, {"x-dimension": (ipp.INTEGER, 21000),
                                                                 "y-dimension": (ipp.INTEGER, 29700)}),
                             "media-source": (ipp.KEYWORD, "main")}),
                         Attr("printer-geo-location", ipp.UNKNOWN, None),
                     ])])
    resp = ipp.decode_response(raw)
    assert resp.ok and resp.request_id == 7
    p = resp.printer
    assert p["printer-state"] == 3
    assert p["marker-levels"] == [92, 1, 1, 12, 89]
    assert p["marker-names"] == ["Cyan ink", "Black ink"]
    assert p["color-supported"] is True
    assert p["copies-supported"] == (1, 99)
    assert str(p["printer-resolution-default"]) == "360dpi"
    assert p["media-col-default"] == {"media-size": {"x-dimension": 21000, "y-dimension": 29700},
                                      "media-source": "main"}
    assert p["printer-geo-location"] is None


def test_collection_list_values():
    col = {"media-source": (ipp.KEYWORD, "main")}
    raw = _response([(ipp.TAG_PRINTER, [Attr("media-col-ready", ipp.BEG_COLLECTION, [col, col])])])
    assert ipp.decode_response(raw).printer["media-col-ready"] == [{"media-source": "main"}] * 2


def test_error_status_and_truncation():
    assert not ipp.decode_response(_response([], status=0x0400)).ok
    with pytest.raises(ipp.IPPError):
        ipp.decode_response(_response([(ipp.TAG_PRINTER, [Attr("x", ipp.TEXT, "abc")])])[:-4])


def test_as_list():
    assert ipp.as_list(None) == [] and ipp.as_list(3) == [3] and ipp.as_list([1]) == [1]


def test_client_posts_ipp(monkeypatch):
    import httpx
    captured = {}

    def handler(request):
        captured["ct"] = request.headers["content-type"]
        captured["url"] = str(request.url)
        return httpx.Response(200, content=_response([(ipp.TAG_PRINTER, [Attr("printer-state", ipp.ENUM, 4)])]))

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))
    c = ipp.IPPClient("printer.local")
    assert c.get_printer_attributes()["printer-state"] == 4
    assert captured == {"ct": "application/ipp", "url": "http://printer.local:631/ipp/print"}
