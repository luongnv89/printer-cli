"""A deliberately tiny SNMPv1 GET client (BER encode/decode) — enough for printer counters.

Avoids pysnmp, whose API changes between releases and Python versions.
"""

from __future__ import annotations

import os
import socket
from typing import Any

INTEGER, OCTET_STRING, NULL, OID, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
COUNTER32, GAUGE32, TIMETICKS, COUNTER64 = 0x41, 0x42, 0x43, 0x46
GET_REQUEST, GET_RESPONSE = 0xA0, 0xA2
NO_SUCH = {0x80, 0x81, 0x82}

# Standard Printer-MIB / MIB-II OIDs
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
PRT_LIFETIME_COUNT = "1.3.6.1.2.1.43.10.2.1.4.1.1"


class SNMPError(Exception):
    pass


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(out)]) + out


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def _int(n: int) -> bytes:
    length = max(1, (n.bit_length() + 8) // 8)
    return _tlv(INTEGER, n.to_bytes(length, "big", signed=True))


def encode_oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.strip(".").split(".")]
    if len(parts) < 2:
        raise SNMPError(f"bad OID {oid!r}")
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(OID, bytes(out))


def decode_oid(raw: bytes) -> str:
    parts = [raw[0] // 40, raw[0] % 40]
    n = 0
    for b in raw[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(n)
            n = 0
    return ".".join(map(str, parts))


def encode_get(oids: list[str], community: str = "public", request_id: int = 1) -> bytes:
    varbinds = b"".join(_tlv(SEQUENCE, encode_oid(o) + _tlv(NULL, b"")) for o in oids)
    pdu = _tlv(GET_REQUEST, _int(request_id) + _int(0) + _int(0) + _tlv(SEQUENCE, varbinds))
    return _tlv(SEQUENCE, _int(0) + _tlv(OCTET_STRING, community.encode()) + pdu)


def _read(data: bytes, pos: int) -> tuple[int, bytes, int]:
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        nbytes = length & 0x7F
        length = int.from_bytes(data[pos : pos + nbytes], "big")
        pos += nbytes
    value = data[pos : pos + length]
    if len(value) != length:
        raise SNMPError("truncated SNMP message")
    return tag, value, pos + length


def _children(data: bytes) -> list[tuple[int, bytes]]:
    out, pos = [], 0
    while pos < len(data):
        tag, value, pos = _read(data, pos)
        out.append((tag, value))
    return out


def _value(tag: int, raw: bytes) -> Any:
    if tag in (INTEGER,):
        return int.from_bytes(raw, "big", signed=True)
    if tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        return int.from_bytes(raw, "big", signed=False)
    if tag == OCTET_STRING:
        return raw
    if tag == OID:
        return decode_oid(raw)
    return None  # NULL, noSuchObject, noSuchInstance, endOfMibView


def decode_response(data: bytes) -> dict[str, Any]:
    tag, msg, _ = _read(data, 0)
    if tag != SEQUENCE:
        raise SNMPError("not an SNMP message")
    parts = _children(msg)
    if len(parts) < 3 or parts[2][0] != GET_RESPONSE:
        raise SNMPError("not a GetResponse")
    pdu = _children(parts[2][1])
    error_status = _value(*pdu[1])
    if error_status:
        raise SNMPError(f"SNMP error-status {error_status}")
    result: dict[str, Any] = {}
    for _, vb in _children(pdu[3][1]):
        (otag, oraw), (vtag, vraw) = _children(vb)[:2]
        result[decode_oid(oraw)] = None if vtag in NO_SUCH else _value(vtag, vraw)
    return result


def get(
    host: str, oids: list[str], community: str = "public", timeout: float = 2.0, retries: int = 1, port: int = 161
) -> dict[str, Any]:
    request_id = int.from_bytes(os.urandom(3), "big")
    packet = encode_get(oids, community, request_id)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        for _ in range(retries + 1):
            try:
                sock.sendto(packet, (host, port))
                data, _ = sock.recvfrom(65535)
                return decode_response(data)
            except socket.timeout:
                continue
    raise SNMPError(f"no SNMP response from {host}")
