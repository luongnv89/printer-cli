"""Minimal, dependency-free IPP/1.1–2.0 codec and HTTP client (RFC 8010 / RFC 8011).

Only what a printer dashboard needs: encode a request, decode any response
(including collections and 1setOf values), and POST it over http/https.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

# --- Tags ------------------------------------------------------------------

TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04
TAG_UNSUPPORTED_GROUP = 0x05
DELIMITERS = {TAG_OPERATION, TAG_JOB, TAG_END, TAG_PRINTER, TAG_UNSUPPORTED_GROUP, 0x06, 0x07, 0x08, 0x09}

UNSUPPORTED = 0x10
UNKNOWN = 0x12
NO_VALUE = 0x13
INTEGER = 0x21
BOOLEAN = 0x22
ENUM = 0x23
OCTET_STRING = 0x30
DATETIME = 0x31
RESOLUTION = 0x32
RANGE = 0x33
BEG_COLLECTION = 0x34
TEXT_WITH_LANG = 0x35
NAME_WITH_LANG = 0x36
END_COLLECTION = 0x37
TEXT = 0x41
NAME = 0x42
KEYWORD = 0x44
URI = 0x45
URI_SCHEME = 0x46
CHARSET = 0x47
LANGUAGE = 0x48
MIME = 0x49
MEMBER_NAME = 0x4A

TRACE = False  # set by `prt --verbose`: one line per request/response on stderr

OP_NAMES = {0x0002: "Print-Job", 0x0004: "Validate-Job", 0x0008: "Cancel-Job", 0x0009: "Get-Job-Attributes",
            0x000A: "Get-Jobs", 0x000B: "Get-Printer-Attributes", 0x003C: "Identify-Printer"}


def trace(message: str) -> None:
    if TRACE:
        import sys
        sys.stderr.write(f"· {message}\n")


STRING_TAGS = {TEXT, NAME, KEYWORD, URI, URI_SCHEME, CHARSET, LANGUAGE, MIME, MEMBER_NAME, OCTET_STRING}

# --- Operations & status ----------------------------------------------------

OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B
OP_IDENTIFY_PRINTER = 0x003C

PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}
JOB_STATES = {
    3: "pending", 4: "pending-held", 5: "processing", 6: "processing-stopped",
    7: "canceled", 8: "aborted", 9: "completed",
}


class IPPError(Exception):
    """An IPP-level failure (bad status code or malformed message)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Resolution:
    x: int
    y: int
    units: int = 3  # 3 = dpi, 4 = dots per cm

    def __str__(self) -> str:
        unit = "dpi" if self.units == 3 else "dpcm"
        return f"{self.x}{unit}" if self.x == self.y else f"{self.x}x{self.y}{unit}"


@dataclass
class Attr:
    """An attribute to encode. ``tag`` selects the wire syntax; ``value`` may be a list."""

    name: str
    tag: int
    value: Any


@dataclass
class Response:
    version: tuple[int, int]
    status: int
    request_id: int
    groups: list[tuple[int, dict[str, Any]]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status < 0x0100

    def group(self, tag: int) -> dict[str, Any]:
        """Merge every group with this delimiter tag (first wins)."""
        merged: dict[str, Any] = {}
        for gtag, attrs in self.groups:
            if gtag == tag:
                for k, v in attrs.items():
                    merged.setdefault(k, v)
        return merged

    def groups_of(self, tag: int) -> list[dict[str, Any]]:
        return [attrs for gtag, attrs in self.groups if gtag == tag]

    @property
    def printer(self) -> dict[str, Any]:
        return self.group(TAG_PRINTER)

    @property
    def operation(self) -> dict[str, Any]:
        return self.group(TAG_OPERATION)


# --- Encoding ----------------------------------------------------------------


def _encode_value(tag: int, value: Any) -> bytes:
    if tag in (INTEGER, ENUM):
        return struct.pack(">i", int(value))
    if tag == BOOLEAN:
        return b"\x01" if value else b"\x00"
    if tag in (NO_VALUE, UNKNOWN, UNSUPPORTED):
        return b""
    if tag == RANGE:
        lo, hi = value
        return struct.pack(">ii", lo, hi)
    if tag == RESOLUTION:
        r = value if isinstance(value, Resolution) else Resolution(*value)
        return struct.pack(">iib", r.x, r.y, r.units)
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _encode_attr(name: str, tag: int, value: Any) -> bytes:
    values = value if isinstance(value, list) else [value]
    out = bytearray()
    for i, v in enumerate(values):
        n = name.encode() if i == 0 else b""
        if tag == BEG_COLLECTION:
            out += struct.pack(">BH", BEG_COLLECTION, len(n)) + n + struct.pack(">H", 0)
            for member_name, (member_tag, member_value) in v.items():
                mn = member_name.encode()
                out += struct.pack(">BHH", MEMBER_NAME, 0, len(mn)) + mn
                member = _encode_attr("", member_tag, member_value)
                out += member
            out += struct.pack(">BHH", END_COLLECTION, 0, 0)
        else:
            data = _encode_value(tag, v)
            out += struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(data)) + data
    return bytes(out)


def encode_request(
    operation: int,
    printer_uri: str,
    *,
    request_id: int = 1,
    version: tuple[int, int] = (1, 1),
    operation_attrs: list[Attr] | None = None,
    job_attrs: list[Attr] | None = None,
    data: bytes = b"",
) -> bytes:
    """Build a complete IPP request message (header + groups + optional document data)."""
    out = bytearray(struct.pack(">BBHI", version[0], version[1], operation, request_id))
    out.append(TAG_OPERATION)
    out += _encode_attr("attributes-charset", CHARSET, "utf-8")
    out += _encode_attr("attributes-natural-language", LANGUAGE, "en")
    out += _encode_attr("printer-uri", URI, printer_uri)
    for a in operation_attrs or []:
        out += _encode_attr(a.name, a.tag, a.value)
    if job_attrs:
        out.append(TAG_JOB)
        for a in job_attrs:
            out += _encode_attr(a.name, a.tag, a.value)
    out.append(TAG_END)
    return bytes(out) + data


# --- Decoding ----------------------------------------------------------------


def _decode_value(tag: int, raw: bytes) -> Any:
    if tag in (INTEGER, ENUM):
        return struct.unpack(">i", raw)[0] if len(raw) == 4 else None
    if tag == BOOLEAN:
        return bool(raw[0]) if raw else False
    if tag in (NO_VALUE, UNKNOWN, UNSUPPORTED):
        return None
    if tag == RANGE and len(raw) == 8:
        return struct.unpack(">ii", raw)
    if tag == RESOLUTION and len(raw) == 9:
        return Resolution(*struct.unpack(">iib", raw))
    if tag == DATETIME and len(raw) == 11:
        y, mo, d, h, mi, s, ds, sign, oh, om = struct.unpack(">HBBBBBBcBB", raw)
        offset = timedelta(hours=oh, minutes=om) * (-1 if sign == b"-" else 1)
        return datetime(y, mo, d, h, mi, s, ds * 100000, tzinfo=timezone(offset))
    if tag in (TEXT_WITH_LANG, NAME_WITH_LANG) and len(raw) >= 4:
        lang_len = struct.unpack(">H", raw[:2])[0]
        text_len = struct.unpack(">H", raw[2 + lang_len : 4 + lang_len])[0]
        return raw[4 + lang_len : 4 + lang_len + text_len].decode("utf-8", "replace")
    # Strings and octetStrings: decode as UTF-8 text (printer octetStrings are textual in practice).
    return raw.decode("utf-8", "replace")


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise IPPError("truncated IPP message")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def peek(self) -> int:
        if self.pos >= len(self.data):
            raise IPPError("truncated IPP message")
        return self.data[self.pos]


def _read_collection(r: _Reader) -> dict[str, Any]:
    """Read members after a begCollection header until the matching endCollection."""
    result: dict[str, Any] = {}
    current: str | None = None
    while True:
        tag = r.u8()
        r.take(r.u16())  # name (always empty inside collections)
        raw = r.take(r.u16())
        if tag == END_COLLECTION:
            return result
        if tag == MEMBER_NAME:
            current = raw.decode("utf-8", "replace")
            continue
        value = _read_collection(r) if tag == BEG_COLLECTION else _decode_value(tag, raw)
        if current is None:
            continue
        if current in result:  # additional value of a 1setOf member
            existing = result[current]
            result[current] = (existing if isinstance(existing, list) else [existing]) + [value]
        else:
            result[current] = value


def decode_response(data: bytes) -> Response:
    """Decode an IPP response. Multi-valued attributes become lists."""
    r = _Reader(data)
    major, minor = r.u8(), r.u8()
    status = r.u16()
    request_id = struct.unpack(">I", r.take(4))[0]
    resp = Response((major, minor), status, request_id)
    group: dict[str, Any] | None = None
    last_name: str | None = None
    while r.pos < len(data):
        tag = r.u8()
        if tag == TAG_END:
            break
        if tag in DELIMITERS:
            group = {}
            resp.groups.append((tag, group))
            last_name = None
            continue
        if group is None:
            raise IPPError("attribute outside of a group")
        name = r.take(r.u16()).decode("utf-8", "replace")
        raw = r.take(r.u16())
        value = _read_collection(r) if tag == BEG_COLLECTION else _decode_value(tag, raw)
        if name:
            last_name = name
            group[name] = value
        elif last_name is not None:  # additional value → 1setOf
            existing = group[last_name]
            group[last_name] = (existing if isinstance(existing, list) else [existing]) + [value]
    return resp


def as_list(value: Any) -> list[Any]:
    """Normalise a decoded attribute to a list (1setOf with one value decodes as a scalar)."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# --- HTTP client --------------------------------------------------------------


class IPPClient:
    """Synchronous IPP over HTTP(S). TLS verification is skipped only for this printer host,
    because embedded printers ship self-signed certificates."""

    def __init__(self, host: str, port: int = 631, path: str = "/ipp/print", tls: bool = False,
                 timeout: float = 5.0):
        self.host, self.port, self.path, self.tls = host, port, path, tls
        self.timeout = timeout
        self._request_id = 0

    @property
    def printer_uri(self) -> str:
        scheme = "ipps" if self.tls else "ipp"
        return f"{scheme}://{self.host}:{self.port}{self.path}"

    @property
    def http_url(self) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{self.host}:{self.port}{self.path}"

    def request(self, operation: int, *, operation_attrs: list[Attr] | None = None,
                job_attrs: list[Attr] | None = None, data: bytes = b"",
                timeout: float | None = None) -> Response:
        self._request_id += 1
        body = encode_request(operation, self.printer_uri, request_id=self._request_id,
                              operation_attrs=operation_attrs, job_attrs=job_attrs, data=data)
        trace(f"IPP → {OP_NAMES.get(operation, hex(operation))} {self.http_url} ({len(body)} bytes)")
        with httpx.Client(verify=False, timeout=timeout or self.timeout) as client:
            http = client.post(self.http_url, content=body,
                               headers={"Content-Type": "application/ipp"})
        if http.status_code != 200:
            raise IPPError(f"HTTP {http.status_code} from {self.http_url}")
        resp = decode_response(http.content)
        trace(f"IPP ← status 0x{resp.status:04x} ({len(http.content)} bytes)")
        if not resp.ok:
            msg = resp.operation.get("status-message") or f"IPP status 0x{resp.status:04x}"
            raise IPPError(str(msg), resp.status)
        return resp

    def get_printer_attributes(self, requested: list[str] | None = None) -> dict[str, Any]:
        resp = self.request(OP_GET_PRINTER_ATTRIBUTES, operation_attrs=[
            Attr("requested-attributes", KEYWORD, requested or ["all"]),
        ])
        return resp.printer
