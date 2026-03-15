#!/usr/bin/env python3
"""
Analyze a pcapng capture and try to locate HTTP multipart uploads (e.g. POST /upload)
and extract the uploaded file bytes from the TCP client->server stream.

This is intentionally dependency-free (no tshark/scapy/dpkt required).
Works for pcapng LINKTYPE_ETHERNET (1) with IPv4/TCP traffic.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import struct
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


PCAPNG_SHB = 0x0A0D0D0A
PCAPNG_IDB = 0x00000001
PCAPNG_EPB = 0x00000006
PCAPNG_SPB = 0x00000003

LINKTYPE_ETHERNET = 1


def _u16be(b: bytes, off: int) -> int:
    return struct.unpack_from(">H", b, off)[0]


def _u32be(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def _ipv4_to_str(b4: bytes) -> str:
    return ".".join(str(x) for x in b4)


@dataclasses.dataclass(frozen=True)
class Packet:
    ip_src: str
    ip_dst: str
    tcp_sport: int
    tcp_dport: int
    seq: int
    payload: bytes


def iter_pcapng_packets(path: Path) -> Iterable[bytes]:
    """
    Yield raw captured packet bytes from pcapng.
    Only supports EPB and SPB blocks.
    """
    data = path.read_bytes()
    pos = 0
    endian = "<"  # will be corrected by SHB BOM
    interfaces: List[int] = []

    while pos + 12 <= len(data):
        block_type, block_len = struct.unpack_from(endian + "II", data, pos)
        if block_len < 12 or pos + block_len > len(data):
            break

        body = data[pos + 8 : pos + block_len - 4]

        if block_type == PCAPNG_SHB:
            # Byte-order magic is first 4 bytes of SHB body.
            if len(body) >= 4:
                bom = struct.unpack_from(endian + "I", body, 0)[0]
                if bom == 0x1A2B3C4D:
                    endian = "<"
                elif bom == 0x4D3C2B1A:
                    endian = ">"
        elif block_type == PCAPNG_IDB:
            if len(body) >= 8:
                linktype = struct.unpack_from(endian + "H", body, 0)[0]
                interfaces.append(linktype)
        elif block_type == PCAPNG_EPB:
            if len(body) >= 20:
                interface_id = struct.unpack_from(endian + "I", body, 0)[0]
                caplen = struct.unpack_from(endian + "I", body, 12)[0]
                pkt_off = 20
                pkt = body[pkt_off : pkt_off + caplen]
                linktype = interfaces[interface_id] if interface_id < len(interfaces) else None
                if linktype == LINKTYPE_ETHERNET:
                    yield pkt
        elif block_type == PCAPNG_SPB:
            # SPB doesn't include interface id; assume interface 0.
            if len(body) >= 4 and (interfaces[0] if interfaces else LINKTYPE_ETHERNET) == LINKTYPE_ETHERNET:
                pkt_len = struct.unpack_from(endian + "I", body, 0)[0]
                pkt = body[4 : 4 + pkt_len]
                yield pkt

        pos += block_len


def decode_ethernet_ipv4_tcp(pkt: bytes) -> Optional[Packet]:
    # Ethernet II header: 14 bytes.
    if len(pkt) < 14:
        return None
    ethertype = _u16be(pkt, 12)
    if ethertype != 0x0800:
        return None

    ip = pkt[14:]
    if len(ip) < 20:
        return None
    ver_ihl = ip[0]
    version = ver_ihl >> 4
    ihl = (ver_ihl & 0x0F) * 4
    if version != 4 or ihl < 20 or len(ip) < ihl:
        return None
    proto = ip[9]
    if proto != 6:
        return None
    total_len = _u16be(ip, 2)
    if total_len < ihl:
        return None
    ip_src = _ipv4_to_str(ip[12:16])
    ip_dst = _ipv4_to_str(ip[16:20])

    tcp = ip[ihl:total_len]
    if len(tcp) < 20:
        return None
    sport = _u16be(tcp, 0)
    dport = _u16be(tcp, 2)
    seq = _u32be(tcp, 4)
    data_off = (tcp[12] >> 4) * 4
    if data_off < 20 or len(tcp) < data_off:
        return None
    payload = tcp[data_off:]
    if not payload:
        return None
    return Packet(ip_src=ip_src, ip_dst=ip_dst, tcp_sport=sport, tcp_dport=dport, seq=seq, payload=payload)


FlowKey = Tuple[str, int, str, int]  # (src_ip, src_port, dst_ip, dst_port) direction-specific


def reassemble_tcp_stream(segments: List[Tuple[int, bytes]]) -> Tuple[bytes, int]:
    """
    Naive TCP reassembly by sequence number.
    Returns (stream_bytes, gap_count).
    """
    segments = [(s, p) for (s, p) in segments if p]
    if not segments:
        return b"", 0
    segments.sort(key=lambda x: x[0])

    base = segments[0][0]
    cur_end = base
    out = bytearray()
    gaps = 0

    for seq, payload in segments:
        if seq > cur_end:
            gaps += 1
            # We don't know missing bytes; skip forward.
            cur_end = seq
        if seq < cur_end:
            overlap = cur_end - seq
            if overlap >= len(payload):
                continue
            payload = payload[overlap:]
        out.extend(payload)
        cur_end += len(payload)

    return bytes(out), gaps


_RE_HEADER_SPLIT = re.compile(br"\r\n")


def parse_http_requests(stream: bytes) -> List[Tuple[int, bytes]]:
    """
    Find HTTP request start offsets and return list of (offset, request_line_bytes).
    """
    out: List[Tuple[int, bytes]] = []
    for m in re.finditer(br"(?:^|\r\n)(GET|POST|PUT|DELETE|HEAD|OPTIONS) ([^ ]+) HTTP/1\.[01]\r\n", stream):
        # request line starts after optional leading CRLF
        start = m.start(1) - (2 if stream[m.start(1) - 2 : m.start(1)] == b"\r\n" else 0)
        line_end = stream.find(b"\r\n", m.start(1))
        if line_end != -1:
            out.append((m.start(1), stream[m.start(1) : line_end]))
    return out


def parse_http_headers(stream: bytes, start: int) -> Optional[Tuple[int, Dict[bytes, bytes]]]:
    """
    Parse HTTP headers beginning at start (method token position).
    Returns (body_start_offset, headers_dict_lowercase_bytes) or None.
    """
    hdr_end = stream.find(b"\r\n\r\n", start)
    if hdr_end == -1:
        return None
    header_blob = stream[start:hdr_end]
    lines = _RE_HEADER_SPLIT.split(header_blob)
    if not lines:
        return None
    headers: Dict[bytes, bytes] = {}
    for line in lines[1:]:
        if not line or b":" not in line:
            continue
        k, v = line.split(b":", 1)
        headers[k.strip().lower()] = v.strip()
    return hdr_end + 4, headers


def _sig_name(b: bytes) -> str:
    if b.startswith(b"PK\x03\x04") or b.startswith(b"PK\x05\x06") or b.startswith(b"PK\x07\x08"):
        return "zip"
    if b.startswith(b"7z\xBC\xAF\x27\x1C"):
        return "7z"
    if b.startswith(b"Rar!\x1A\x07\x00") or b.startswith(b"Rar!\x1A\x07\x01\x00"):
        return "rar"
    if b.startswith(b"\x1F\x8B\x08"):
        return "gzip"
    return "unknown"


@dataclasses.dataclass
class UploadExtract:
    flow: FlowKey
    uri: str
    filename: str
    content_type: str
    content_length: int
    extracted_len: int
    signature: str
    gap_count: int
    complete: bool
    out_path: Optional[Path]


def try_extract_multipart_file(stream: bytes, req_start: int, headers: Dict[bytes, bytes]) -> Optional[Tuple[str, str, bytes, int]]:
    """
    Return (uri, filename, file_bytes, content_length) if found.
    """
    # Parse request line for URI.
    line_end = stream.find(b"\r\n", req_start)
    if line_end == -1:
        return None
    req_line = stream[req_start:line_end]
    parts = req_line.split(b" ")
    if len(parts) < 2:
        return None
    uri = parts[1].decode("utf-8", "replace")

    ctype = headers.get(b"content-type", b"").decode("utf-8", "replace")
    if "multipart/form-data" not in ctype.lower():
        return None
    m = re.search(r"boundary=([^;]+)", ctype, flags=re.I)
    if not m:
        return None
    boundary = m.group(1).strip().strip('"').encode("utf-8", "replace")
    boundary_marker = b"--" + boundary

    body_start = stream.find(b"\r\n\r\n", req_start)
    if body_start == -1:
        return None
    body_start += 4

    # Find first boundary.
    b0 = stream.find(boundary_marker, body_start)
    if b0 == -1:
        return None

    # Find the part that contains name="file".
    cursor = b0
    while True:
        # Each part starts with boundary line, then CRLF, then headers, then CRLFCRLF then data.
        part_hdr_start = stream.find(b"\r\n", cursor)
        if part_hdr_start == -1:
            return None
        part_hdr_start += 2
        part_hdr_end = stream.find(b"\r\n\r\n", part_hdr_start)
        if part_hdr_end == -1:
            return None
        part_headers = stream[part_hdr_start:part_hdr_end]
        if b'name="file"' in part_headers or b"name=file" in part_headers:
            # Extract filename if present.
            fn = "unknown.bin"
            mfn = re.search(br'filename=\"([^\"]+)\"', part_headers)
            if mfn:
                fn = mfn.group(1).decode("utf-8", "replace")
            data_start = part_hdr_end + 4
            # File data ends before \r\n--boundary
            next_boundary = stream.find(b"\r\n" + boundary_marker, data_start)
            if next_boundary == -1:
                return None
            file_bytes = stream[data_start:next_boundary]
            clen = 0
            try:
                clen = int(headers.get(b"content-length", b"0").decode("ascii", "ignore") or "0")
            except ValueError:
                clen = 0
            return uri, fn, file_bytes, clen

        # Advance to next part boundary.
        next_part = stream.find(boundary_marker, part_hdr_end + 4)
        if next_part == -1:
            return None
        cursor = next_part


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcapng", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("extracted_from_pcap"))
    ap.add_argument("--write", action="store_true", help="Write extracted file bytes to --out-dir")
    args = ap.parse_args()

    segments_by_flow: Dict[FlowKey, List[Tuple[int, bytes]]] = {}
    for raw in iter_pcapng_packets(args.pcapng):
        p = decode_ethernet_ipv4_tcp(raw)
        if p is None:
            continue
        fk: FlowKey = (p.ip_src, p.tcp_sport, p.ip_dst, p.tcp_dport)
        segments_by_flow.setdefault(fk, []).append((p.seq, p.payload))

    extracts: List[UploadExtract] = []

    for fk, segs in segments_by_flow.items():
        stream, gaps = reassemble_tcp_stream(segs)
        if not stream:
            continue
        # Fast prefilter.
        if b"POST " not in stream or b"HTTP/1." not in stream:
            continue

        # Locate requests.
        for m in re.finditer(br"(POST) ([^ ]+) HTTP/1\.[01]\r\n", stream):
            req_start = m.start(1)
            parsed = parse_http_headers(stream, req_start)
            if parsed is None:
                continue
            body_off, headers = parsed
            # Only care about upload-ish endpoints.
            uri = m.group(2).decode("utf-8", "replace")
            if uri not in ("/upload", "/peer/upload"):
                continue
            hit = try_extract_multipart_file(stream, req_start, headers)
            if hit is None:
                continue
            uri_s, filename, file_bytes, content_len = hit
            sig = _sig_name(file_bytes[:16])
            complete = True
            # If we know content-length, verify we at least captured the body fully.
            if content_len and (body_off + content_len) > len(stream):
                complete = False

            out_path = None
            if args.write:
                args.out_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "upload.bin"
                out_path = args.out_dir / safe
                out_path.write_bytes(file_bytes)

            extracts.append(
                UploadExtract(
                    flow=fk,
                    uri=uri_s,
                    filename=filename,
                    content_type=headers.get(b"content-type", b"").decode("utf-8", "replace"),
                    content_length=content_len,
                    extracted_len=len(file_bytes),
                    signature=sig,
                    gap_count=gaps,
                    complete=complete,
                    out_path=out_path,
                )
            )

    if not extracts:
        print("NO_MATCH: did not find a complete multipart file upload in the capture.")
        print("HINT: ensure you captured on the receiving PC interface and the upload used HTTP (not HTTPS).")
        return 2

    for ex in extracts:
        src_ip, src_port, dst_ip, dst_port = ex.flow
        print("MATCH")
        print(f"flow={src_ip}:{src_port} -> {dst_ip}:{dst_port}")
        print(f"uri={ex.uri}")
        print(f"filename={ex.filename}")
        print(f"content_type={ex.content_type}")
        print(f"content_length={ex.content_length}")
        print(f"extracted_len={ex.extracted_len}")
        print(f"signature={ex.signature}")
        print(f"tcp_gap_count={ex.gap_count}")
        print(f"complete={ex.complete}")
        print(f"written_to={str(ex.out_path) if ex.out_path else ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

