"""Image metadata containers, preservation checks and safe output primitives.

No GUI, model calls or credential access. Callers own the save transaction;
this module preserves unrelated image metadata and verifies written bytes.
"""

import hashlib
import io
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime

from PIL import Image
import piexif
import piexif.helper


def _escape_xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_output_filename(title, fallback="seo_image", max_len=120, suffix=".jpg"):
    """Create a cross-platform safe filename while preserving non-Latin text."""
    suffix = str(suffix or ".jpg").lower()
    if suffix not in {".jpg", ".png", ".webp"}:
        raise ValueError(f"Unsupported output filename suffix: {suffix}")
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', " ", title or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    base = "_".join(cleaned.split()[:8]).rstrip(" ._")
    if not base:
        base = fallback
    if len(base) > max_len:
        base = base[:max_len].rstrip(" ._")
    return f"{base}{suffix}"


_SAFE_OUTPUT_PATH_LOCK = threading.Lock()
_RESERVED_OUTPUT_PATHS = set()
_MAX_SAFE_OUTPUT_NAME_ATTEMPTS = 10000
_JPEG_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_JPEG_EXTENDED_XMP_HEADER = b"http://ns.adobe.com/xmp/extension/\x00"
_JPEG_EXIF_HEADER = b"Exif\x00\x00"
_JPEG_PHOTOSHOP_HEADER = b"Photoshop 3.0\x00"
_JPEG_ICC_HEADER = b"ICC_PROFILE\x00"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_XMP_KEYWORD = b"XML:com.adobe.xmp"
_WEBP_RIFF_HEADER = b"RIFF"
_WEBP_FORM_TYPE = b"WEBP"
_WEBP_VP8X_ICCP_FLAG = 0x20
_WEBP_VP8X_ALPHA_FLAG = 0x10
_WEBP_VP8X_EXIF_FLAG = 0x08
_WEBP_VP8X_XMP_FLAG = 0x04
_WEBP_VP8X_ANIMATION_FLAG = 0x02
_TIFF_SIGNATURES = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_TIFF_TYPE_SIZES = {
    1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1,
    8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4,
}


def _is_tiff_container(data):
    return isinstance(data, (bytes, bytearray)) and bytes(data[:4]) in _TIFF_SIGNATURES


def _unsupported_tiff_save_error(image_path):
    return (
        f"{Path(image_path).name} (TIFF/TIF): "
        "\u5f53\u524d\u7248\u672c\u6682\u4e0d\u652f\u6301\u5b89\u5168\u4fdd\u5b58 TIFF \u56fe\u7247\u3002"
        "\u539f\u59cb TIFF \u672a\u88ab\u4fee\u6539\uff1b\u4f60\u4ecd\u53ef\u5bfc\u51fa JSON \u6216 CSV\u3002"
    )
_XMP_NAMESPACES = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
}
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

for _xmp_prefix, _xmp_uri in _XMP_NAMESPACES.items():
    ET.register_namespace(_xmp_prefix, _xmp_uri)


def _is_png_chunk_type(value):
    return len(value) == 4 and all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in value)


def _build_png_chunk(kind, payload):
    if not _is_png_chunk_type(kind):
        raise ValueError("PNG chunk type is invalid")
    if len(payload) > 0xFFFFFFFF:
        raise ValueError("PNG chunk payload is too large")
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return len(payload).to_bytes(4, "big") + kind + payload + crc.to_bytes(4, "big")


def _parse_png_container(data):
    """Strictly parse a PNG container without decoding or rewriting image data."""
    if not isinstance(data, (bytes, bytearray)) or not data.startswith(_PNG_SIGNATURE):
        raise ValueError("PNG preservation requires a valid PNG signature")

    chunks = []
    pos = len(_PNG_SIGNATURE)
    idat_seen = False
    idat_ended = False
    seen = set()
    color_chunks = {b"iCCP", b"sRGB", b"gAMA", b"cHRM"}
    while pos < len(data):
        if pos + 12 > len(data):
            raise ValueError("PNG chunk header is truncated")
        length = int.from_bytes(data[pos:pos + 4], "big")
        end = pos + 12 + length
        if end > len(data):
            raise ValueError("PNG chunk length exceeds the container")
        kind = bytes(data[pos + 4:pos + 8])
        payload = bytes(data[pos + 8:pos + 8 + length])
        raw = bytes(data[pos:end])
        if not _is_png_chunk_type(kind):
            raise ValueError("PNG chunk type is invalid")
        expected_crc = int.from_bytes(data[pos + 8 + length:end], "big")
        if expected_crc != (zlib.crc32(kind + payload) & 0xFFFFFFFF):
            raise ValueError(f"PNG {kind.decode('ascii')} chunk CRC is invalid")
        if kind[0] & 0x20 == 0 and kind not in {b"IHDR", b"PLTE", b"IDAT", b"IEND"}:
            raise ValueError(f"PNG contains an unsupported critical chunk: {kind.decode('ascii')}")
        if kind == b"IHDR":
            if chunks or kind in seen or length != 13:
                raise ValueError("PNG IHDR structure is invalid")
            width = int.from_bytes(payload[:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth = payload[8]
            color_type = payload[9]
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if not width or not height or bit_depth not in valid_depths.get(color_type, set()):
                raise ValueError("PNG IHDR semantics are invalid")
        elif kind == b"PLTE":
            if kind in seen or idat_seen or length == 0 or length % 3:
                raise ValueError("PNG PLTE structure is invalid")
        elif kind == b"IDAT":
            if idat_ended:
                raise ValueError("PNG IDAT chunks must be consecutive")
            idat_seen = True
        else:
            if idat_seen:
                idat_ended = True
        if kind == b"IEND":
            if kind in seen or length != 0 or end != len(data) or not idat_seen:
                raise ValueError("PNG IEND structure is invalid")
        if kind in color_chunks and kind in seen:
            raise ValueError(f"PNG contains duplicate {kind.decode('ascii')} color metadata")
        seen.add(kind)
        chunks.append({"kind": kind, "payload": payload, "raw": raw})
        pos = end

    if not chunks or chunks[-1]["kind"] != b"IEND":
        raise ValueError("PNG has no valid IEND chunk")
    return chunks


def _parse_png_itxt(payload):
    try:
        keyword, remainder = payload.split(b"\x00", 1)
        if not keyword or len(remainder) < 2:
            raise ValueError
        compressed, method = remainder[:2]
        language, remainder = remainder[2:].split(b"\x00", 1)
        translated, text = remainder.split(b"\x00", 1)
    except ValueError as exc:
        raise ValueError("PNG iTXt chunk is malformed") from exc
    if compressed not in (0, 1) or method != 0:
        raise ValueError("PNG iTXt compression fields are invalid")
    if compressed:
        try:
            text = zlib.decompress(text)
        except zlib.error as exc:
            raise ValueError("PNG iTXt compressed text cannot be decoded") from exc
    return keyword, language, translated, text, bool(compressed)


def _build_png_itxt(keyword, language, translated, text, compressed=False):
    if not keyword or b"\x00" in keyword or b"\x00" in language or b"\x00" in translated:
        raise ValueError("PNG iTXt text fields are invalid")
    if compressed:
        text = zlib.compress(text)
    return keyword + b"\x00" + bytes([int(compressed), 0]) + language + b"\x00" + translated + b"\x00" + text


def _build_webp_chunk(kind, payload):
    if not isinstance(kind, bytes) or len(kind) != 4:
        raise ValueError("WebP RIFF chunk type is invalid")
    if len(payload) > 0xFFFFFFFF:
        raise ValueError("WebP RIFF chunk payload is too large")
    return kind + len(payload).to_bytes(4, "little") + payload + (b"\x00" if len(payload) % 2 else b"")


def _parse_webp_container(data):
    """Strictly parse a RIFF/WebP container while retaining every original chunk."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        raise ValueError("WebP preservation requires a valid RIFF container")
    if data[:4] != _WEBP_RIFF_HEADER or data[8:12] != _WEBP_FORM_TYPE:
        raise ValueError("WebP preservation requires RIFF/WEBP headers")
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise ValueError("WebP RIFF size does not match the container")

    chunks = []
    pos = 12
    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError("WebP RIFF chunk header is truncated")
        kind = bytes(data[pos:pos + 4])
        length = int.from_bytes(data[pos + 4:pos + 8], "little")
        payload_end = pos + 8 + length
        end = payload_end + (length % 2)
        if end > len(data):
            raise ValueError("WebP RIFF chunk length exceeds the container")
        padding = bytes(data[payload_end:end])
        if padding and padding != b"\x00":
            raise ValueError("WebP RIFF chunk padding is invalid")
        chunks.append(
            {
                "kind": kind,
                "payload": bytes(data[pos + 8:payload_end]),
                "raw": bytes(data[pos:end]),
            }
        )
        pos = end
    if pos != len(data):
        raise ValueError("WebP RIFF chunk boundaries are invalid")

    kinds = [chunk["kind"] for chunk in chunks]
    if kinds.count(b"VP8X") > 1 or (b"VP8X" in kinds and kinds[0] != b"VP8X"):
        raise ValueError("WebP VP8X structure is invalid")
    if b"VP8X" in kinds and len(chunks[0]["payload"]) != 10:
        raise ValueError("WebP VP8X payload is invalid")
    for metadata_kind in (b"ICCP", b"EXIF", b"XMP ", b"ALPH"):
        if kinds.count(metadata_kind) > 1:
            raise ValueError(f"WebP contains duplicate {metadata_kind.decode('ascii')} chunks")

    flags = chunks[0]["payload"][0] if chunks and chunks[0]["kind"] == b"VP8X" else 0
    animated = b"ANIM" in kinds or b"ANMF" in kinds or bool(flags & _WEBP_VP8X_ANIMATION_FLAG)
    if animated:
        return chunks, True

    image_chunks = [kind for kind in kinds if kind in {b"VP8 ", b"VP8L"}]
    if len(image_chunks) != 1:
        raise ValueError("Static WebP requires exactly one VP8 or VP8L image payload")
    if b"ALPH" in kinds and image_chunks[0] != b"VP8 ":
        raise ValueError("WebP ALPH is only valid with a VP8 payload")
    metadata_present = any(kind in kinds for kind in (b"ICCP", b"EXIF", b"XMP ", b"ALPH"))
    if metadata_present and not kinds[:1] == [b"VP8X"]:
        raise ValueError("WebP metadata requires VP8X feature flags")
    if kinds[:1] == [b"VP8X"]:
        for bit, kind in (
            (_WEBP_VP8X_ICCP_FLAG, b"ICCP"),
            (_WEBP_VP8X_EXIF_FLAG, b"EXIF"),
            (_WEBP_VP8X_XMP_FLAG, b"XMP "),
        ):
            if bool(flags & bit) != bool(kind in kinds):
                raise ValueError(f"WebP VP8X flag does not match {kind.decode('ascii')} presence")
    return chunks, False


def _assemble_webp_container(chunks):
    payload = b"".join(_build_webp_chunk(chunk["kind"], chunk["payload"]) for chunk in chunks)
    return _WEBP_RIFF_HEADER + (len(payload) + 4).to_bytes(4, "little") + _WEBP_FORM_TYPE + payload


def _normalized_output_path(path):
    """Return a Windows-safe comparison key for existing and future output paths."""
    try:
        normalized = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        normalized = Path(os.path.abspath(os.path.normpath(os.fspath(path))))
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(normalized))))


def _reserve_safe_output_path(base_dir, filename, input_paths, worker_reservations):
    """Reserve a new JPEG output path that cannot replace any current input."""
    output_dir = Path(base_dir).expanduser()
    if not output_dir.is_dir():
        raise OSError(f"Cannot create a safe output path: output directory is unavailable: {output_dir}")

    try:
        output_dir = output_dir.resolve(strict=False)
    except (OSError, RuntimeError):
        output_dir = Path(os.path.abspath(os.path.normpath(os.fspath(output_dir))))

    filename_path = Path(filename)
    if filename_path.name != filename or not filename_path.stem:
        raise ValueError(f"Cannot create a safe output path from filename: {filename!r}")

    source_keys = {_normalized_output_path(path) for path in input_paths}
    stem = filename_path.stem
    suffix = filename_path.suffix or ".jpg"

    with _SAFE_OUTPUT_PATH_LOCK:
        for index in range(_MAX_SAFE_OUTPUT_NAME_ATTEMPTS):
            name = filename if index == 0 else f"{stem}_{index}{suffix}"
            candidate = output_dir / name
            candidate_key = _normalized_output_path(candidate)
            if (
                candidate_key not in source_keys
                and candidate_key not in worker_reservations
                and candidate_key not in _RESERVED_OUTPUT_PATHS
                and not candidate.exists()
            ):
                worker_reservations.add(candidate_key)
                _RESERVED_OUTPUT_PATHS.add(candidate_key)
                return candidate
    raise OSError(f"Cannot create a safe output path after {_MAX_SAFE_OUTPUT_NAME_ATTEMPTS} filename conflicts")


def _release_safe_output_paths(worker_reservations):
    with _SAFE_OUTPUT_PATH_LOCK:
        _RESERVED_OUTPUT_PATHS.difference_update(worker_reservations)


def _build_jpeg_segment(marker, payload):
    if not 0xE0 <= marker <= 0xEF:
        raise ValueError(f"Unsupported JPEG metadata marker: {marker:#x}")
    if len(payload) + 2 > 0xFFFF:
        raise ValueError(f"JPEG metadata segment is too large: {len(payload)} bytes")
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _parse_jpeg_container(data):
    """Return pre-scan JPEG segments and the untouched SOS-to-EOI byte sequence."""
    if not isinstance(data, (bytes, bytearray)) or data[:2] != b"\xff\xd8":
        raise ValueError("JPEG metadata merge requires a valid SOI marker")

    pos = 2
    segments = []
    standalone_markers = {*range(0xD0, 0xD8), 0x01}
    while pos < len(data):
        if pos + 1 >= len(data) or data[pos] != 0xFF:
            raise ValueError("Malformed JPEG marker before scan data")
        marker = data[pos + 1]
        if marker == 0xDA:
            scan_data = bytes(data[pos:])
            if b"\xff\xd9" not in scan_data:
                raise ValueError("JPEG scan data has no EOI marker")
            return segments, scan_data
        if marker == 0xD9:
            raise ValueError("JPEG reached EOI before SOS marker")
        if marker in standalone_markers:
            segments.append((marker, bytes(data[pos:pos + 2]), b""))
            pos += 2
            continue
        if pos + 4 > len(data):
            raise ValueError("Truncated JPEG metadata segment header")
        segment_length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if segment_length < 2:
            raise ValueError("Invalid JPEG metadata segment length")
        end = pos + 2 + segment_length
        if end > len(data):
            raise ValueError("Truncated JPEG metadata segment payload")
        raw_segment = bytes(data[pos:end])
        segments.append((marker, raw_segment, bytes(data[pos + 4:end])))
        pos = end
    raise ValueError("JPEG has no SOS marker")


def _jpeg_segment_type(marker, payload):
    if marker == 0xE1 and payload.startswith(_JPEG_EXIF_HEADER):
        return "exif"
    if marker == 0xE1 and payload.startswith(_JPEG_XMP_HEADER):
        return "xmp"
    if marker == 0xE1 and payload.startswith(_JPEG_EXTENDED_XMP_HEADER):
        return "extended_xmp"
    if marker == 0xED and payload.startswith(_JPEG_PHOTOSHOP_HEADER):
        return "photoshop"
    if marker == 0xE2 and payload.startswith(_JPEG_ICC_HEADER):
        return "icc"
    return "other"


def _managed_exif_tags(description="", keywords=None, creator=""):
    tags = {0x9C9B}
    if description:
        tags.add(0x010E)
    if keywords:
        tags.add(0x9C9D)
    if creator:
        tags.add(0x013B)
    return tags


def _managed_iptc_datasets(description="", keywords=None, creator="", date_created=""):
    datasets = {(2, 5)}
    if description:
        datasets.add((2, 120))
    if keywords:
        datasets.add((2, 25))
    if creator:
        datasets.add((2, 80))
    if _parse_metadata_date(date_created):
        datasets.add((2, 55))
    return datasets


def _empty_exif_tiff():
    return b"II*\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00"


def _tiff_type_size(tag_type):
    try:
        return _TIFF_TYPE_SIZES[tag_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported EXIF TIFF field type: {tag_type}") from exc


def _parse_tiff_ifd(tiff, endian, offset):
    if offset < 8 or offset + 2 > len(tiff):
        raise ValueError("EXIF IFD offset is out of bounds")
    count = struct.unpack_from(endian + "H", tiff, offset)[0]
    end = offset + 2 + count * 12 + 4
    if end > len(tiff):
        raise ValueError("EXIF IFD table is truncated")
    entries = []
    for index in range(count):
        entry_offset = offset + 2 + index * 12
        tag, tag_type, value_count = struct.unpack_from(endian + "HHI", tiff, entry_offset)
        value_field = bytes(tiff[entry_offset + 8:entry_offset + 12])
        value_size = _tiff_type_size(tag_type) * value_count
        if value_size <= 4:
            value = value_field[:value_size]
        else:
            value_offset = struct.unpack(endian + "I", value_field)[0]
            if value_offset > len(tiff) or value_offset + value_size > len(tiff):
                raise ValueError(f"EXIF tag {tag:#x} points outside the TIFF payload")
            value = bytes(tiff[value_offset:value_offset + value_size])
        entries.append(
            {
                "tag": tag,
                "type": tag_type,
                "count": value_count,
                "value_field": value_field,
                "value": value,
            }
        )
    next_ifd = struct.unpack_from(endian + "I", tiff, end - 4)[0]
    return entries, next_ifd


def _validate_exif_tiff(tiff):
    if len(tiff) < 14 or tiff[:2] not in (b"II", b"MM"):
        raise ValueError("EXIF TIFF header is invalid")
    endian = "<" if tiff[:2] == b"II" else ">"
    if struct.unpack_from(endian + "H", tiff, 2)[0] != 42:
        raise ValueError("EXIF TIFF magic is invalid")
    ifd0_offset = struct.unpack_from(endian + "I", tiff, 4)[0]
    ifd0_entries, next_ifd = _parse_tiff_ifd(tiff, endian, ifd0_offset)

    nested_offsets = []
    for entry in ifd0_entries:
        if entry["tag"] in (0x8769, 0x8825) and entry["type"] in (4, 13) and entry["count"] == 1:
            nested_offsets.append(struct.unpack(endian + "I", entry["value"])[0])
    for nested_offset in nested_offsets:
        _parse_tiff_ifd(tiff, endian, nested_offset)
    if next_ifd:
        _parse_tiff_ifd(tiff, endian, next_ifd)
    return endian, ifd0_entries, next_ifd


def _tiff_ascii(value):
    return value.encode("utf-8") + b"\x00"


def _append_tiff_value(buffer, value):
    if len(value) <= 4:
        return value.ljust(4, b"\x00")
    if len(buffer) % 2:
        buffer.extend(b"\x00")
    offset = len(buffer)
    buffer.extend(value)
    if offset > 0xFFFFFFFF:
        raise ValueError("EXIF TIFF value offset is too large")
    return offset


def _merge_exif_payload(payload, title, description, keywords, creator=""):
    """Append a new IFD0 so all original TIFF data and unmanaged tags stay intact."""
    if payload is None:
        tiff = _empty_exif_tiff()
    else:
        if not payload.startswith(_JPEG_EXIF_HEADER):
            raise ValueError("EXIF APP1 payload has an invalid header")
        tiff = payload[len(_JPEG_EXIF_HEADER):]

    endian, entries, next_ifd = _validate_exif_tiff(tiff)
    managed_values = {0x9C9B: (1, title.encode("utf-16le") + b"\x00\x00")}
    if description:
        managed_values[0x010E] = (2, _tiff_ascii(description))
    if keywords:
        managed_values[0x9C9D] = (1, ";".join(keywords).encode("utf-16le") + b"\x00\x00")
    if creator:
        managed_values[0x013B] = (2, _tiff_ascii(creator))

    replacements = {}
    unmanaged_entries = []
    for entry in entries:
        tag = entry["tag"]
        if tag in managed_values:
            if tag in replacements:
                raise ValueError(f"EXIF has duplicate managed tag: {tag:#x}")
            replacements[tag] = managed_values.pop(tag)
        else:
            unmanaged_entries.append((tag, entry["type"], entry["count"], entry["value_field"]))

    rebuilt = bytearray(tiff)
    merged_entries = list(unmanaged_entries)
    for tag, (tag_type, value) in replacements.items():
        value_field = _append_tiff_value(rebuilt, value)
        if isinstance(value_field, int):
            value_field = struct.pack(endian + "I", value_field)
        merged_entries.append((tag, tag_type, len(value), value_field))
    for tag, (tag_type, value) in managed_values.items():
        value_field = _append_tiff_value(rebuilt, value)
        if isinstance(value_field, int):
            value_field = struct.pack(endian + "I", value_field)
        merged_entries.append((tag, tag_type, len(value), value_field))

    merged_entries.sort(key=lambda item: item[0])
    if len(merged_entries) > 0xFFFF:
        raise ValueError("EXIF IFD0 has too many entries")
    if len(rebuilt) % 2:
        rebuilt.extend(b"\x00")
    new_ifd0_offset = len(rebuilt)
    rebuilt.extend(struct.pack(endian + "H", len(merged_entries)))
    for tag, tag_type, value_count, value_field in merged_entries:
        rebuilt.extend(struct.pack(endian + "HHI", tag, tag_type, value_count))
        rebuilt.extend(value_field)
    rebuilt.extend(struct.pack(endian + "I", next_ifd))
    if new_ifd0_offset > 0xFFFFFFFF:
        raise ValueError("EXIF IFD0 offset is too large")
    rebuilt[4:8] = struct.pack(endian + "I", new_ifd0_offset)
    return _JPEG_EXIF_HEADER + bytes(rebuilt)


def _xmp_qname(prefix, name):
    return f"{{{_XMP_NAMESPACES[prefix]}}}{name}"


def _new_xmp_root():
    root = ET.Element(_xmp_qname("x", "xmpmeta"))
    rdf = ET.SubElement(root, _xmp_qname("rdf", "RDF"))
    ET.SubElement(rdf, _xmp_qname("rdf", "Description"))
    return root


def _xmp_descriptions(root):
    descriptions = root.findall(".//" + _xmp_qname("rdf", "Description"))
    if not descriptions:
        rdf = root.find(".//" + _xmp_qname("rdf", "RDF"))
        if rdf is None:
            raise ValueError("XMP packet has no RDF container")
        descriptions = [ET.SubElement(rdf, _xmp_qname("rdf", "Description"))]
    return descriptions


def _xmp_property(root, qname):
    matches = []
    for description in _xmp_descriptions(root):
        for child in list(description):
            if child.tag == qname:
                matches.append((description, child))
    if len(matches) > 1:
        raise ValueError(f"XMP has multiple managed properties: {qname}")
    if matches:
        return matches[0]
    return _xmp_descriptions(root)[0], None


def _set_xmp_alt(root, local_name, value):
    qname = _xmp_qname("dc", local_name)
    description, prop = _xmp_property(root, qname)
    if prop is None:
        prop = ET.SubElement(description, qname)
        alt = ET.SubElement(prop, _xmp_qname("rdf", "Alt"))
    else:
        alts = [child for child in list(prop) if child.tag == _xmp_qname("rdf", "Alt")]
        if len(alts) != 1:
            raise ValueError(f"XMP {local_name} is not a single RDF Alt value")
        alt = alts[0]
    values = [child for child in list(alt) if child.tag == _xmp_qname("rdf", "li")]
    default = next((item for item in values if item.attrib.get(_XML_LANG) == "x-default"), None)
    if default is None:
        default = ET.SubElement(alt, _xmp_qname("rdf", "li"), {_XML_LANG: "x-default"})
    default.text = value


def _set_xmp_keywords(root, keywords):
    qname = _xmp_qname("dc", "subject")
    description, prop = _xmp_property(root, qname)
    if prop is None:
        prop = ET.SubElement(description, qname)
        bag = ET.SubElement(prop, _xmp_qname("rdf", "Bag"))
    else:
        bags = [child for child in list(prop) if child.tag == _xmp_qname("rdf", "Bag")]
        if len(bags) != 1:
            raise ValueError("XMP keywords are not a single RDF Bag")
        bag = bags[0]
    for item in list(bag):
        if item.tag == _xmp_qname("rdf", "li"):
            bag.remove(item)
    for keyword in keywords:
        ET.SubElement(bag, _xmp_qname("rdf", "li")).text = keyword


def _set_xmp_creator(root, creator):
    qname = _xmp_qname("dc", "creator")
    description, prop = _xmp_property(root, qname)
    if prop is None:
        prop = ET.SubElement(description, qname)
        sequence = ET.SubElement(prop, _xmp_qname("rdf", "Seq"))
    else:
        sequences = [child for child in list(prop) if child.tag == _xmp_qname("rdf", "Seq")]
        if len(sequences) != 1:
            raise ValueError("XMP creator is not a single RDF Seq")
        sequence = sequences[0]
    names = [child for child in list(sequence) if child.tag == _xmp_qname("rdf", "li")]
    if names:
        names[0].text = creator
    else:
        ET.SubElement(sequence, _xmp_qname("rdf", "li")).text = creator


def _set_xmp_text(root, prefix, local_name, value):
    qname = _xmp_qname(prefix, local_name)
    description, prop = _xmp_property(root, qname)
    if prop is None:
        prop = ET.SubElement(description, qname)
    elif list(prop):
        raise ValueError(f"XMP {local_name} has an unsupported structured value")
    prop.text = value


def _merge_xmp_payload(payload, title, description, keywords, creator="", date_created=""):
    if payload is None:
        root = _new_xmp_root()
    else:
        if not payload.startswith(_JPEG_XMP_HEADER):
            raise ValueError("XMP APP1 payload has an invalid header")
        try:
            root = ET.fromstring(payload[len(_JPEG_XMP_HEADER):])
        except ET.ParseError as exc:
            raise ValueError(f"XMP packet cannot be parsed safely: {exc}") from exc
        if root.find(".//" + _xmp_qname("rdf", "RDF")) is None:
            raise ValueError("XMP packet has no RDF container")

    _set_xmp_alt(root, "title", title)
    if description:
        _set_xmp_alt(root, "description", description)
    if keywords:
        _set_xmp_keywords(root, keywords)
    if creator:
        _set_xmp_creator(root, creator)

    date_meta = _parse_metadata_date(date_created)
    if date_meta:
        _set_xmp_text(root, "photoshop", "DateCreated", date_meta["xmp_date"])
        _set_xmp_text(root, "xmp", "CreateDate", date_meta["xmp_datetime"])
    return _JPEG_XMP_HEADER + ET.tostring(root, encoding="utf-8")


def _parse_photoshop_resources(payload):
    if not payload.startswith(_JPEG_PHOTOSHOP_HEADER):
        raise ValueError("APP13 does not use the Photoshop resource header")
    data = payload[len(_JPEG_PHOTOSHOP_HEADER):]
    resources = []
    pos = 0
    while pos < len(data):
        start = pos
        if pos + 7 > len(data) or data[pos:pos + 4] != b"8BIM":
            raise ValueError("Photoshop APP13 resource block is malformed")
        resource_id = int.from_bytes(data[pos + 4:pos + 6], "big")
        name_length = data[pos + 6]
        name_end = pos + 7 + name_length
        if name_end > len(data):
            raise ValueError("Photoshop APP13 resource name is truncated")
        if (1 + name_length) % 2:
            name_end += 1
        if name_end + 4 > len(data):
            raise ValueError("Photoshop APP13 resource length is truncated")
        data_length = int.from_bytes(data[name_end:name_end + 4], "big")
        payload_start = name_end + 4
        payload_end = payload_start + data_length
        if payload_end > len(data):
            raise ValueError("Photoshop APP13 resource data is truncated")
        end = payload_end + (data_length % 2)
        if end > len(data):
            raise ValueError("Photoshop APP13 resource padding is truncated")
        resources.append(
            {
                "id": resource_id,
                "name_field": bytes(data[pos + 6:name_end]),
                "data": bytes(data[payload_start:payload_end]),
                "raw": bytes(data[start:end]),
            }
        )
        pos = end
    return resources


def _build_photoshop_resource(resource_id, name_field, data):
    if not name_field or name_field[0] + 1 > len(name_field):
        raise ValueError("Photoshop resource name field is invalid")
    padding = b"\x00" if len(data) % 2 else b""
    return (
        b"8BIM"
        + resource_id.to_bytes(2, "big")
        + name_field
        + len(data).to_bytes(4, "big")
        + data
        + padding
    )


def _parse_iptc_records(data):
    records = []
    pos = 0
    while pos < len(data):
        if pos + 5 > len(data) or data[pos] != 0x1C:
            raise ValueError("IPTC IIM record is malformed")
        record, dataset = data[pos + 1], data[pos + 2]
        short_length = int.from_bytes(data[pos + 3:pos + 5], "big")
        pos += 5
        if short_length & 0x8000:
            length_size = short_length & 0x7FFF
            if not 1 <= length_size <= 4 or pos + length_size > len(data):
                raise ValueError("IPTC IIM extended length is malformed")
            value_length = int.from_bytes(data[pos:pos + length_size], "big")
            prefix_end = pos + length_size
            value_start = prefix_end
        else:
            value_length = short_length
            prefix_end = pos
            value_start = pos
        value_end = value_start + value_length
        if value_end > len(data):
            raise ValueError("IPTC IIM value is truncated")
        records.append(
            {
                "record": record,
                "dataset": dataset,
                "value": bytes(data[value_start:value_end]),
                "raw": bytes(data[pos - 5:value_end]),
                "prefix_end": prefix_end,
            }
        )
        pos = value_end
    return records


def _build_iptc_record(record, dataset, value):
    if len(value) > 0x7FFF:
        raise ValueError("IPTC value is too large")
    return b"\x1c" + bytes([record, dataset]) + len(value).to_bytes(2, "big") + value


def _merge_iptc_iim(data, title, description, keywords, creator="", date_created=""):
    records = _parse_iptc_records(data)
    managed_datasets = _managed_iptc_datasets(description, keywords, creator, date_created)
    preserved = [
        entry["raw"]
        for entry in records
        if (entry["record"], entry["dataset"]) not in managed_datasets
    ]
    charset_present = any((entry["record"], entry["dataset"]) == (1, 90) for entry in records)
    additions = []
    if not charset_present:
        additions.append(_build_iptc_record(1, 90, b"\x1b%G"))
    additions.append(_build_iptc_record(2, 5, title.encode("utf-8")[:64]))
    if keywords:
        additions.extend(
            _build_iptc_record(2, 25, keyword.strip().encode("utf-8")[:64])
            for keyword in keywords
            if keyword.strip()
        )
    if description:
        additions.append(_build_iptc_record(2, 120, description.encode("utf-8")[:2000]))
    if creator:
        additions.append(_build_iptc_record(2, 80, creator.encode("utf-8")[:32]))
    date_meta = _parse_metadata_date(date_created)
    if date_meta:
        additions.append(_build_iptc_record(2, 55, date_meta["iptc_date"].encode("ascii")))
    return b"".join(preserved + additions)


def _new_iptc_resource(title, description, keywords, creator="", date_created=""):
    return _build_photoshop_resource(
        0x0404,
        b"\x00\x00",
        _merge_iptc_iim(b"", title, description, keywords, creator, date_created),
    )


def _merge_photoshop_payload(payload, title, description, keywords, creator="", date_created=""):
    resources = _parse_photoshop_resources(payload)
    iptc_indices = [index for index, resource in enumerate(resources) if resource["id"] == 0x0404]
    if len(iptc_indices) > 1:
        raise ValueError("Photoshop APP13 contains multiple IPTC resources")

    merged = []
    iptc_index = iptc_indices[0] if iptc_indices else None
    for index, resource in enumerate(resources):
        if index != iptc_index:
            merged.append(resource["raw"])
            continue
        merged_data = _merge_iptc_iim(resource["data"], title, description, keywords, creator, date_created)
        merged.append(_build_photoshop_resource(resource["id"], resource["name_field"], merged_data))
    if iptc_index is None:
        merged.append(_new_iptc_resource(title, description, keywords, creator, date_created))
    return _JPEG_PHOTOSHOP_HEADER + b"".join(merged)


def _merge_jpeg_metadata(data, title, description, keywords, creator="", date_created=""):
    """Merge managed fields into one JPEG while retaining all unmodified container bytes."""
    segments, scan_data = _parse_jpeg_container(data)
    exif_indices = []
    xmp_indices = []
    photoshop_indices = []
    for index, (marker, _raw, payload) in enumerate(segments):
        segment_type = _jpeg_segment_type(marker, payload)
        if segment_type == "extended_xmp":
            raise ValueError("Extended XMP cannot be safely merged in this JPEG route")
        if segment_type == "exif":
            exif_indices.append(index)
        elif segment_type == "xmp":
            xmp_indices.append(index)
        elif segment_type == "photoshop":
            photoshop_indices.append(index)

    if len(exif_indices) > 1:
        raise ValueError("JPEG contains multiple EXIF APP1 segments")
    if len(xmp_indices) > 1:
        raise ValueError("JPEG contains multiple standard XMP APP1 segments")

    exif_payload = segments[exif_indices[0]][2] if exif_indices else None
    xmp_payload = segments[xmp_indices[0]][2] if xmp_indices else None
    merged_exif = _merge_exif_payload(exif_payload, title, description, keywords, creator)
    merged_xmp = _merge_xmp_payload(xmp_payload, title, description, keywords, creator, date_created)

    merged_photoshop = {}
    iptc_written = False
    for index in photoshop_indices:
        if iptc_written:
            resources = _parse_photoshop_resources(segments[index][2])
            if any(resource["id"] == 0x0404 for resource in resources):
                raise ValueError("JPEG contains IPTC data in multiple APP13 segments")
            merged_photoshop[index] = segments[index][2]
            continue
        merged_photoshop[index] = _merge_photoshop_payload(
            segments[index][2], title, description, keywords, creator, date_created
        )
        iptc_written = True

    output_segments = []
    for index, (marker, raw_segment, payload) in enumerate(segments):
        if index in exif_indices:
            output_segments.append(_build_jpeg_segment(0xE1, merged_exif))
        elif index in xmp_indices:
            output_segments.append(_build_jpeg_segment(0xE1, merged_xmp))
        elif index in merged_photoshop:
            output_segments.append(_build_jpeg_segment(0xED, merged_photoshop[index]))
        else:
            output_segments.append(raw_segment)
    if not exif_indices:
        output_segments.append(_build_jpeg_segment(0xE1, merged_exif))
    if not xmp_indices:
        output_segments.append(_build_jpeg_segment(0xE1, merged_xmp))
    if not photoshop_indices:
        output_segments.append(
            _build_jpeg_segment(
                0xED,
                _JPEG_PHOTOSHOP_HEADER + _new_iptc_resource(
                    title, description, keywords, creator, date_created
                ),
            )
        )
    return b"\xff\xd8" + b"".join(output_segments) + scan_data


def _merge_embedded_exif_payload(payload, title, description, keywords, creator=""):
    """Merge a raw TIFF EXIF payload used by PNG eXIf and WebP EXIF chunks."""
    source = None if payload is None else _JPEG_EXIF_HEADER + payload
    merged = _merge_exif_payload(source, title, description, keywords, creator)
    return merged[len(_JPEG_EXIF_HEADER):]


def _merge_embedded_xmp_packet(payload, title, description, keywords, creator="", date_created=""):
    """Merge a raw XMP packet used by PNG iTXt and WebP XMP chunks."""
    source = None if payload is None else _JPEG_XMP_HEADER + payload
    merged = _merge_xmp_payload(source, title, description, keywords, creator, date_created)
    return merged[len(_JPEG_XMP_HEADER):]


def _merge_png_metadata(data, title, description, keywords, creator="", date_created=""):
    """Merge only managed metadata into a PNG while preserving image/container bytes."""
    chunks = _parse_png_container(data)
    exif_indices = [index for index, chunk in enumerate(chunks) if chunk["kind"] == b"eXIf"]
    xmp_entries = []
    for index, chunk in enumerate(chunks):
        if chunk["kind"] != b"iTXt":
            continue
        keyword, language, translated, text, compressed = _parse_png_itxt(chunk["payload"])
        if keyword == _PNG_XMP_KEYWORD:
            xmp_entries.append((index, language, translated, text, compressed))
    if len(exif_indices) > 1:
        raise ValueError("PNG contains multiple eXIf chunks and cannot be merged safely")
    if len(xmp_entries) > 1:
        raise ValueError("PNG contains multiple XMP iTXt chunks and cannot be merged safely")

    exif_payload = chunks[exif_indices[0]]["payload"] if exif_indices else None
    xmp_payload = xmp_entries[0][3] if xmp_entries else None
    merged_exif = _merge_embedded_exif_payload(exif_payload, title, description, keywords, creator)
    merged_xmp = _merge_embedded_xmp_packet(xmp_payload, title, description, keywords, creator, date_created)
    xmp_by_index = {entry[0]: entry for entry in xmp_entries}

    result = bytearray(_PNG_SIGNATURE)
    inserted_missing = False
    for index, chunk in enumerate(chunks):
        if chunk["kind"] == b"IDAT" and not inserted_missing:
            if not exif_indices:
                result.extend(_build_png_chunk(b"eXIf", merged_exif))
            if not xmp_entries:
                result.extend(
                    _build_png_chunk(
                        b"iTXt",
                        _build_png_itxt(_PNG_XMP_KEYWORD, b"", b"", merged_xmp),
                    )
                )
            inserted_missing = True
        if index in exif_indices:
            result.extend(_build_png_chunk(b"eXIf", merged_exif))
        elif index in xmp_by_index:
            _index, language, translated, _text, compressed = xmp_by_index[index]
            result.extend(
                _build_png_chunk(
                    b"iTXt",
                    _build_png_itxt(_PNG_XMP_KEYWORD, language, translated, merged_xmp, compressed),
                )
            )
        else:
            result.extend(chunk["raw"])
    if not inserted_missing:
        raise ValueError("PNG has no IDAT chunk for metadata placement")
    return bytes(result)


def _webp_dimensions(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "WEBP":
                raise ValueError("WebP container did not decode as WEBP")
            width, height = image.size
            has_alpha = "A" in image.getbands()
            image.load()
    except Exception as exc:
        raise ValueError(f"WebP image dimensions cannot be verified: {exc}") from exc
    if width <= 0 or height <= 0 or width > 0x1000000 or height > 0x1000000:
        raise ValueError("WebP canvas dimensions are invalid")
    return width, height, has_alpha


def _new_webp_vp8x_payload(width, height, flags):
    return (
        bytes([flags])
        + b"\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )


def _merge_webp_metadata(data, title, description, keywords, creator="", date_created=""):
    """Merge static WebP EXIF/XMP chunks without changing image payload chunks."""
    chunks, animated = _parse_webp_container(data)
    if animated:
        raise ValueError("Current version does not support safe saving of animated WebP")

    kinds = [chunk["kind"] for chunk in chunks]
    exif_indices = [index for index, chunk in enumerate(chunks) if chunk["kind"] == b"EXIF"]
    xmp_indices = [index for index, chunk in enumerate(chunks) if chunk["kind"] == b"XMP "]
    if len(exif_indices) > 1 or len(xmp_indices) > 1:
        raise ValueError("WebP contains duplicate managed metadata chunks")
    merged_exif = _merge_embedded_exif_payload(
        chunks[exif_indices[0]]["payload"] if exif_indices else None,
        title,
        description,
        keywords,
        creator,
    )
    merged_xmp = _merge_embedded_xmp_packet(
        chunks[xmp_indices[0]]["payload"] if xmp_indices else None,
        title,
        description,
        keywords,
        creator,
        date_created,
    )

    if kinds and kinds[0] == b"VP8X":
        vp8x_payload = bytearray(chunks[0]["payload"])
        vp8x_payload[0] |= _WEBP_VP8X_EXIF_FLAG | _WEBP_VP8X_XMP_FLAG
    else:
        width, height, has_alpha = _webp_dimensions(data)
        flags = _WEBP_VP8X_EXIF_FLAG | _WEBP_VP8X_XMP_FLAG
        if b"ICCP" in kinds:
            flags |= _WEBP_VP8X_ICCP_FLAG
        if has_alpha:
            flags |= _WEBP_VP8X_ALPHA_FLAG
        vp8x_payload = bytearray(_new_webp_vp8x_payload(width, height, flags))
        chunks.insert(0, {"kind": b"VP8X", "payload": bytes(vp8x_payload), "raw": b""})
        exif_indices = [index + 1 for index in exif_indices]
        xmp_indices = [index + 1 for index in xmp_indices]

    output = []
    for index, chunk in enumerate(chunks):
        if chunk["kind"] == b"VP8X":
            output.append({"kind": b"VP8X", "payload": bytes(vp8x_payload), "raw": b""})
        elif index in exif_indices:
            output.append({"kind": b"EXIF", "payload": merged_exif, "raw": b""})
        elif index in xmp_indices:
            output.append({"kind": b"XMP ", "payload": merged_xmp, "raw": b""})
        else:
            output.append(chunk)
    if not exif_indices:
        output.append({"kind": b"EXIF", "payload": merged_exif, "raw": b""})
    if not xmp_indices:
        output.append({"kind": b"XMP ", "payload": merged_xmp, "raw": b""})
    return _assemble_webp_container(output)


def _write_container_bytes_to_tempfile(container_bytes, directory, prefix):
    fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(directory))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_path, "wb") as target:
            target.write(container_bytes)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        _cleanup_tempfile(tmp_path)
        raise
    return tmp_path


def _write_jpeg_bytes_to_tempfile(jpeg_bytes, directory):
    fd, tmp_name = tempfile.mkstemp(prefix=".pixtagra_jpeg_", suffix=".tmp", dir=str(directory))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_path, "wb") as target:
            target.write(jpeg_bytes)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        _cleanup_tempfile(tmp_path)
        raise
    return tmp_path


def _write_jpeg_to_tempfile(image, directory, exif_bytes):
    fd, tmp_name = tempfile.mkstemp(prefix=".pixtagra_save_", suffix=".tmp", dir=str(directory))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        image.save(tmp_path, "JPEG", exif=exif_bytes, quality=95)
        _fsync_file(tmp_path)
    except Exception:
        _cleanup_tempfile(tmp_path)
        raise
    return tmp_path


def _write_metadata_to_tempfile(directory, metadata_bytes):
    fd, tmp_name = tempfile.mkstemp(prefix=".pixtagra_metadata_", suffix=".tmp", dir=str(directory))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_path, "wb") as target:
            target.write(metadata_bytes)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        _cleanup_tempfile(tmp_path)
        raise
    return tmp_path


def _fsync_file(path):
    with open(path, "rb+") as target:
        target.flush()
        os.fsync(target.fileno())


def _cleanup_tempfile(tmp_path):
    if not tmp_path:
        return None
    try:
        Path(tmp_path).unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"Temporary file cleanup failed: {tmp_path}: {exc}"
    return None


def _publish_new_output(tmp_path, save_path):
    """Atomically link a complete staged file without replacing an existing target."""
    os.link(tmp_path, save_path)


def _decode_exif_text(value, encoding="utf-8"):
    if isinstance(value, tuple):
        value = bytes(value)
    if isinstance(value, bytes):
        if encoding.lower() == "utf-16le":
            while value.endswith(b"\x00\x00"):
                value = value[:-2]
        else:
            value = value.rstrip(b"\x00")
        return value.decode(encoding, errors="replace")
    return str(value or "")


def _jpeg_payloads(data, marker, prefix):
    segments, _scan_data = _parse_jpeg_container(data)
    return [payload for found_marker, _raw, payload in segments if found_marker == marker and payload.startswith(prefix)]


def _xmp_root_from_payload(payload):
    if not payload.startswith(_JPEG_XMP_HEADER):
        raise ValueError("XMP APP1 payload has an invalid header")
    try:
        return ET.fromstring(payload[len(_JPEG_XMP_HEADER):])
    except ET.ParseError as exc:
        raise ValueError(f"XMP packet cannot be parsed safely: {exc}") from exc


def _xmp_alt_text(root, local_name, language="x-default"):
    qname = _xmp_qname("dc", local_name)
    matches = root.findall(
        ".//" + qname + "/" + _xmp_qname("rdf", "Alt") + "/" + _xmp_qname("rdf", "li")
    )
    for item in matches:
        if item.attrib.get(_XML_LANG) == language:
            return item.text or ""
    return ""


def _xmp_keyword_values(root):
    return [
        item.text or ""
        for item in root.findall(
            ".//" + _xmp_qname("dc", "subject") + "/" + _xmp_qname("rdf", "Bag") + "/" + _xmp_qname("rdf", "li")
        )
    ]


def _xmp_without_managed_fields(payload, description="", keywords=None, creator="", date_created=""):
    root = _xmp_root_from_payload(payload)
    removable = {_xmp_qname("dc", "title")}
    if description:
        removable.add(_xmp_qname("dc", "description"))
    if keywords:
        removable.add(_xmp_qname("dc", "subject"))
    if creator:
        removable.add(_xmp_qname("dc", "creator"))
    if _parse_metadata_date(date_created):
        removable.update(
            {
                _xmp_qname("photoshop", "DateCreated"),
                _xmp_qname("xmp", "CreateDate"),
            }
        )
    for description in _xmp_descriptions(root):
        for child in list(description):
            if child.tag in removable:
                description.remove(child)
    return ET.tostring(root, encoding="utf-8")


def _iptc_records_from_jpeg(data):
    payloads = _jpeg_payloads(data, 0xED, _JPEG_PHOTOSHOP_HEADER)
    records = []
    for payload in payloads:
        for resource in _parse_photoshop_resources(payload):
            if resource["id"] == 0x0404:
                records.extend(_parse_iptc_records(resource["data"]))
    return records


def _iptc_preservation_snapshot(data, description="", keywords=None, creator="", date_created=""):
    payloads = _jpeg_payloads(data, 0xED, _JPEG_PHOTOSHOP_HEADER)
    managed_datasets = _managed_iptc_datasets(description, keywords, creator, date_created)
    snapshot = []
    for payload in payloads:
        resources = _parse_photoshop_resources(payload)
        resource_snapshot = []
        for resource in resources:
            if resource["id"] == 0x0404:
                records = _parse_iptc_records(resource["data"])
                unmanaged_records = [
                    entry["raw"]
                    for entry in records
                    if (entry["record"], entry["dataset"]) not in managed_datasets
                    # The coded-character-set record is structural metadata
                    # required when PixTagra creates a UTF-8 IPTC resource.
                    and (entry["record"], entry["dataset"]) != (1, 90)
                ]
                # A Photoshop block without IPTC receives a new resource containing
                # only PixTagra-managed fields. It is not unmanaged source content.
                if unmanaged_records:
                    resource_snapshot.append(("iptc", unmanaged_records))
            else:
                resource_snapshot.append(("resource", resource["raw"]))
        if resource_snapshot:
            snapshot.append(resource_snapshot)
    return snapshot


def _jpeg_preservation_snapshot(path, description="", keywords=None, creator="", date_created=""):
    data = Path(path).read_bytes()
    segments, scan_data = _parse_jpeg_container(data)
    exif_payloads = [
        payload
        for marker, _raw, payload in segments
        if _jpeg_segment_type(marker, payload) == "exif"
    ]
    xmp_payloads = [
        payload
        for marker, _raw, payload in segments
        if _jpeg_segment_type(marker, payload) == "xmp"
    ]
    if len(exif_payloads) > 1 or len(xmp_payloads) > 1:
        raise ValueError("JPEG preservation verification requires one EXIF and one XMP packet at most")
    if any(_jpeg_segment_type(marker, payload) == "extended_xmp" for marker, _raw, payload in segments):
        raise ValueError("JPEG preservation verification does not support Extended XMP")

    snapshot = {
        "scan_digest": hashlib.sha256(scan_data).digest(),
        "icc_segments": [
            raw
            for marker, raw, payload in segments
            if _jpeg_segment_type(marker, payload) == "icc"
        ],
        "unmanaged_segments": [
            raw
            for marker, raw, payload in segments
            if _jpeg_segment_type(marker, payload) not in {"exif", "xmp", "photoshop"}
        ],
        "iptc": _iptc_preservation_snapshot(
            data, description=description, keywords=keywords, creator=creator, date_created=date_created
        ),
    }
    if exif_payloads:
        tiff = exif_payloads[0][len(_JPEG_EXIF_HEADER):]
        _endian, entries, _next_ifd = _validate_exif_tiff(tiff)
        snapshot["exif_tail"] = tiff[8:]
        snapshot["unmanaged_ifd0"] = [
            (entry["tag"], entry["type"], entry["count"], entry["value"])
            for entry in entries
            if entry["tag"] not in _managed_exif_tags(description, keywords, creator)
        ]
    if xmp_payloads:
        snapshot["xmp_unmanaged"] = _xmp_without_managed_fields(
            xmp_payloads[0], description=description, keywords=keywords, creator=creator, date_created=date_created
        )
    return snapshot


def _verify_jpeg_preservation(source_path, output_path, description="", keywords=None, creator="", date_created=""):
    source = _jpeg_preservation_snapshot(
        source_path, description=description, keywords=keywords, creator=creator, date_created=date_created
    )
    output = _jpeg_preservation_snapshot(
        output_path, description=description, keywords=keywords, creator=creator, date_created=date_created
    )
    if output["scan_digest"] != source["scan_digest"]:
        raise ValueError("JPEG preservation verification failed: compressed scan data changed")
    if output["icc_segments"] != source["icc_segments"]:
        raise ValueError("JPEG preservation verification failed: ICC APP2 segments changed")
    if output["unmanaged_segments"] != source["unmanaged_segments"]:
        raise ValueError("JPEG preservation verification failed: unmanaged JPEG segments changed")
    if output["iptc"] != source["iptc"]:
        raise ValueError("JPEG preservation verification failed: unmanaged IPTC or Photoshop resources changed")

    if "exif_tail" in source:
        output_payloads = _jpeg_payloads(Path(output_path).read_bytes(), 0xE1, _JPEG_EXIF_HEADER)
        if len(output_payloads) != 1:
            raise ValueError("JPEG preservation verification failed: EXIF APP1 is missing")
        output_tiff = output_payloads[0][len(_JPEG_EXIF_HEADER):]
        if output_tiff[8:8 + len(source["exif_tail"])] != source["exif_tail"]:
            raise ValueError("JPEG preservation verification failed: original EXIF TIFF data changed")
        _endian, entries, _next_ifd = _validate_exif_tiff(output_tiff)
        unmanaged_ifd0 = [
            (entry["tag"], entry["type"], entry["count"], entry["value"])
            for entry in entries
            if entry["tag"] not in _managed_exif_tags(description, keywords, creator)
        ]
        if unmanaged_ifd0 != source["unmanaged_ifd0"]:
            raise ValueError("JPEG preservation verification failed: unmanaged EXIF IFD0 fields changed")
    if "xmp_unmanaged" in source:
        output_payloads = _jpeg_payloads(Path(output_path).read_bytes(), 0xE1, _JPEG_XMP_HEADER)
        if len(output_payloads) != 1:
            raise ValueError("JPEG preservation verification failed: XMP APP1 is missing")
        if _xmp_without_managed_fields(
            output_payloads[0],
            description=description,
            keywords=keywords,
            creator=creator,
            date_created=date_created,
        ) != source["xmp_unmanaged"]:
            raise ValueError("JPEG preservation verification failed: unmanaged XMP fields changed")


def _decoded_image_snapshot(path, expected_format):
    target = Path(path)
    with Image.open(target) as verified:
        if verified.format != expected_format:
            raise ValueError(f"Metadata verification failed: expected {expected_format} output")
        verified.verify()
    with Image.open(target) as image:
        if image.format != expected_format:
            raise ValueError(f"Metadata verification failed: expected {expected_format} output")
        width, height = image.size
        mode = image.mode
        image.load()
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        snapshot = {
            "size": (width, height),
            "mode": mode,
            "pixel_digest": hashlib.sha256(rgba.tobytes()).digest(),
            "alpha_digest": hashlib.sha256(alpha.tobytes()).digest(),
        }
        rgba.close()
        alpha.close()
    if width <= 0 or height <= 0:
        raise ValueError(f"Metadata verification failed: invalid image dimensions: {target}")
    return snapshot


def _embedded_exif_snapshot(payload, description="", keywords=None, creator=""):
    _endian, entries, _next_ifd = _validate_exif_tiff(payload)
    return {
        "tail": payload[8:],
        "unmanaged_ifd0": [
            (entry["tag"], entry["type"], entry["count"], entry["value"])
            for entry in entries
            if entry["tag"] not in _managed_exif_tags(description, keywords, creator)
        ],
    }


def _verify_embedded_exif_fields(payload, title, description, keywords, creator=""):
    _endian, entries, _next_ifd = _validate_exif_tiff(payload)
    values = {entry["tag"]: entry["value"] for entry in entries}
    if _decode_exif_text(values.get(0x9C9B), "utf-16le") != title:
        raise ValueError("Metadata verification failed: title is missing")
    if description and _decode_exif_text(values.get(piexif.ImageIFD.ImageDescription)) != description:
        raise ValueError("Metadata verification failed: description is missing")
    if keywords and _decode_exif_text(values.get(0x9C9D), "utf-16le") != ";".join(keywords):
        raise ValueError("Metadata verification failed: keywords are missing")
    if creator and _decode_exif_text(values.get(piexif.ImageIFD.Artist)) != creator:
        raise ValueError("Metadata verification failed: creator is missing")


def _verify_embedded_xmp_fields(payload, title, description, keywords, creator="", date_created=""):
    root = _xmp_root_from_payload(_JPEG_XMP_HEADER + payload)
    if _xmp_alt_text(root, "title") != title:
        raise ValueError("Metadata verification failed: XMP title is missing")
    if description and _xmp_alt_text(root, "description") != description:
        raise ValueError("Metadata verification failed: XMP description is missing")
    if keywords and _xmp_keyword_values(root) != keywords:
        raise ValueError("Metadata verification failed: XMP keywords are missing")
    if creator:
        creator_values = [
            item.text or ""
            for item in root.findall(
                ".//" + _xmp_qname("dc", "creator") + "/" + _xmp_qname("rdf", "Seq") + "/" + _xmp_qname("rdf", "li")
            )
        ]
        if not creator_values or creator_values[0] != creator:
            raise ValueError("Metadata verification failed: XMP creator is missing")
    date_meta = _parse_metadata_date(date_created)
    if date_meta:
        if _xmp_property_text(root, "photoshop", "DateCreated") != date_meta["xmp_date"]:
            raise ValueError("Metadata verification failed: XMP DateCreated is missing")
        if _xmp_property_text(root, "xmp", "CreateDate") != date_meta["xmp_datetime"]:
            raise ValueError("Metadata verification failed: XMP CreateDate is missing")


def _xmp_property_text(root, prefix, local_name):
    qname = _xmp_qname(prefix, local_name)
    matches = root.findall(".//" + qname)
    if len(matches) > 1:
        raise ValueError(f"XMP has multiple {local_name} properties")
    if not matches or list(matches[0]):
        return ""
    return matches[0].text or ""


def _png_xmp_entries(chunks):
    entries = []
    for index, chunk in enumerate(chunks):
        if chunk["kind"] != b"iTXt":
            continue
        keyword, language, translated, text, compressed = _parse_png_itxt(chunk["payload"])
        if keyword == _PNG_XMP_KEYWORD:
            entries.append((index, language, translated, text, compressed))
    if len(entries) > 1:
        raise ValueError("PNG preservation verification found multiple XMP iTXt chunks")
    return entries


def _png_preservation_snapshot(path, description="", keywords=None, creator="", date_created=""):
    data = Path(path).read_bytes()
    chunks = _parse_png_container(data)
    exif_payloads = [chunk["payload"] for chunk in chunks if chunk["kind"] == b"eXIf"]
    if len(exif_payloads) > 1:
        raise ValueError("PNG preservation verification found multiple eXIf chunks")
    xmp_entries = _png_xmp_entries(chunks)
    snapshot = {
        "image": _decoded_image_snapshot(path, "PNG"),
        "idat_digest": hashlib.sha256(
            b"".join(chunk["payload"] for chunk in chunks if chunk["kind"] == b"IDAT")
        ).digest(),
        "icc_payloads": [chunk["payload"] for chunk in chunks if chunk["kind"] == b"iCCP"],
        "protected_chunks": [
            chunk["raw"]
            for chunk in chunks
            if chunk["kind"] != b"eXIf"
            and not (chunk["kind"] == b"iTXt" and _parse_png_itxt(chunk["payload"])[0] == _PNG_XMP_KEYWORD)
        ],
    }
    if exif_payloads:
        snapshot["exif"] = _embedded_exif_snapshot(exif_payloads[0], description, keywords, creator)
    if xmp_entries:
        snapshot["xmp_unmanaged"] = _xmp_without_managed_fields(
            _JPEG_XMP_HEADER + xmp_entries[0][3],
            description=description,
            keywords=keywords,
            creator=creator,
            date_created=date_created,
        )
    return snapshot


def _verify_png_preservation(source_path, output_path, description="", keywords=None, creator="", date_created=""):
    source = _png_preservation_snapshot(source_path, description, keywords, creator, date_created)
    output = _png_preservation_snapshot(output_path, description, keywords, creator, date_created)
    if output["image"] != source["image"]:
        raise ValueError("PNG preservation verification failed: pixels, alpha, dimensions, or mode changed")
    if output["idat_digest"] != source["idat_digest"]:
        raise ValueError("PNG preservation verification failed: IDAT payload changed")
    if output["icc_payloads"] != source["icc_payloads"]:
        raise ValueError("PNG preservation verification failed: ICC profile changed")
    if output["protected_chunks"] != source["protected_chunks"]:
        raise ValueError("PNG preservation verification failed: unmanaged PNG chunks changed")
    if "exif" in source:
        if "exif" not in output:
            raise ValueError("PNG preservation verification failed: eXIf metadata is missing")
        output_payload = [chunk["payload"] for chunk in _parse_png_container(Path(output_path).read_bytes()) if chunk["kind"] == b"eXIf"]
        if len(output_payload) != 1:
            raise ValueError("PNG preservation verification failed: eXIf metadata is missing")
        if output_payload[0][8:8 + len(source["exif"]["tail"])] != source["exif"]["tail"]:
            raise ValueError("PNG preservation verification failed: original eXIf TIFF data changed")
        if output["exif"]["unmanaged_ifd0"] != source["exif"]["unmanaged_ifd0"]:
            raise ValueError("PNG preservation verification failed: unmanaged eXIf fields changed")
    if "xmp_unmanaged" in source:
        if output.get("xmp_unmanaged") != source["xmp_unmanaged"]:
            raise ValueError("PNG preservation verification failed: unmanaged XMP fields changed")


def _verify_png_written_metadata(path, title, description, keywords, creator="", source_png_path=None, date_created=""):
    target = Path(path)
    if not target.is_file() or target.stat().st_size <= 0:
        raise ValueError(f"Metadata verification failed: file is missing or empty: {target}")
    chunks = _parse_png_container(target.read_bytes())
    _decoded_image_snapshot(target, "PNG")
    exif_payloads = [chunk["payload"] for chunk in chunks if chunk["kind"] == b"eXIf"]
    xmp_entries = _png_xmp_entries(chunks)
    if len(exif_payloads) != 1 or len(xmp_entries) != 1:
        raise ValueError("Metadata verification failed: PNG eXIf or XMP metadata is missing")
    _verify_embedded_exif_fields(exif_payloads[0], title, description, keywords, creator)
    _verify_embedded_xmp_fields(xmp_entries[0][3], title, description, keywords, creator, date_created)
    if source_png_path:
        _verify_png_preservation(source_png_path, target, description, keywords, creator, date_created)
    return {"path": str(target), "size": target.stat().st_size}


def _webp_preservation_snapshot(path, description="", keywords=None, creator="", date_created=""):
    data = Path(path).read_bytes()
    chunks, animated = _parse_webp_container(data)
    if animated:
        raise ValueError("WebP preservation verification does not support animation")
    exif_payloads = [chunk["payload"] for chunk in chunks if chunk["kind"] == b"EXIF"]
    xmp_payloads = [chunk["payload"] for chunk in chunks if chunk["kind"] == b"XMP "]
    if len(exif_payloads) > 1 or len(xmp_payloads) > 1:
        raise ValueError("WebP preservation verification found duplicate metadata")
    vp8x_payload = chunks[0]["payload"] if chunks and chunks[0]["kind"] == b"VP8X" else None
    snapshot = {
        "image": _decoded_image_snapshot(path, "WEBP"),
        "image_payloads": [
            (chunk["kind"], chunk["payload"])
            for chunk in chunks
            if chunk["kind"] in {b"VP8 ", b"VP8L", b"ALPH"}
        ],
        "icc_payloads": [chunk["payload"] for chunk in chunks if chunk["kind"] == b"ICCP"],
        "protected_chunks": [
            chunk["raw"]
            for chunk in chunks
            if chunk["kind"] not in {b"VP8X", b"EXIF", b"XMP "}
        ],
        "vp8x_flags": vp8x_payload[0] if vp8x_payload else 0,
        "vp8x_tail": vp8x_payload[1:] if vp8x_payload else None,
    }
    if exif_payloads:
        snapshot["exif"] = _embedded_exif_snapshot(exif_payloads[0], description, keywords, creator)
    if xmp_payloads:
        snapshot["xmp_unmanaged"] = _xmp_without_managed_fields(
            _JPEG_XMP_HEADER + xmp_payloads[0],
            description=description,
            keywords=keywords,
            creator=creator,
            date_created=date_created,
        )
    return snapshot


def _verify_webp_preservation(source_path, output_path, description="", keywords=None, creator="", date_created=""):
    source = _webp_preservation_snapshot(source_path, description, keywords, creator, date_created)
    output = _webp_preservation_snapshot(output_path, description, keywords, creator, date_created)
    if output["image"] != source["image"]:
        raise ValueError("WebP preservation verification failed: pixels, alpha, dimensions, or mode changed")
    if output["image_payloads"] != source["image_payloads"]:
        raise ValueError("WebP preservation verification failed: VP8, VP8L, or ALPH payload changed")
    if output["icc_payloads"] != source["icc_payloads"]:
        raise ValueError("WebP preservation verification failed: ICC profile changed")
    if output["protected_chunks"] != source["protected_chunks"]:
        raise ValueError("WebP preservation verification failed: unmanaged RIFF chunks changed")
    if source["vp8x_tail"] is not None:
        if output["vp8x_tail"] != source["vp8x_tail"]:
            raise ValueError("WebP preservation verification failed: VP8X canvas fields changed")
        if output["vp8x_flags"] & source["vp8x_flags"] != source["vp8x_flags"]:
            raise ValueError("WebP preservation verification failed: VP8X feature flags were removed")
    if "exif" in source:
        output_payloads = [
            chunk["payload"]
            for chunk in _parse_webp_container(Path(output_path).read_bytes())[0]
            if chunk["kind"] == b"EXIF"
        ]
        if len(output_payloads) != 1:
            raise ValueError("WebP preservation verification failed: EXIF metadata is missing")
        if output_payloads[0][8:8 + len(source["exif"]["tail"])] != source["exif"]["tail"]:
            raise ValueError("WebP preservation verification failed: original EXIF TIFF data changed")
        if output.get("exif", {}).get("unmanaged_ifd0") != source["exif"]["unmanaged_ifd0"]:
            raise ValueError("WebP preservation verification failed: unmanaged EXIF fields changed")
    if "xmp_unmanaged" in source and output.get("xmp_unmanaged") != source["xmp_unmanaged"]:
        raise ValueError("WebP preservation verification failed: unmanaged XMP fields changed")


def _verify_webp_written_metadata(path, title, description, keywords, creator="", source_webp_path=None, date_created=""):
    target = Path(path)
    if not target.is_file() or target.stat().st_size <= 0:
        raise ValueError(f"Metadata verification failed: file is missing or empty: {target}")
    chunks, animated = _parse_webp_container(target.read_bytes())
    if animated:
        raise ValueError("Metadata verification failed: output is animated WebP")
    _decoded_image_snapshot(target, "WEBP")
    exif_payloads = [chunk["payload"] for chunk in chunks if chunk["kind"] == b"EXIF"]
    xmp_payloads = [chunk["payload"] for chunk in chunks if chunk["kind"] == b"XMP "]
    if len(exif_payloads) != 1 or len(xmp_payloads) != 1:
        raise ValueError("Metadata verification failed: WebP EXIF or XMP metadata is missing")
    _verify_embedded_exif_fields(exif_payloads[0], title, description, keywords, creator)
    _verify_embedded_xmp_fields(xmp_payloads[0], title, description, keywords, creator, date_created)
    flags = chunks[0]["payload"][0] if chunks and chunks[0]["kind"] == b"VP8X" else 0
    if not flags & _WEBP_VP8X_EXIF_FLAG or not flags & _WEBP_VP8X_XMP_FLAG:
        raise ValueError("Metadata verification failed: WebP VP8X metadata flags are missing")
    if source_webp_path:
        _verify_webp_preservation(source_webp_path, target, description, keywords, creator, date_created)
    return {"path": str(target), "size": target.stat().st_size}


def _verify_written_metadata(
    jpeg_path, title, description, keywords, creator="", source_jpeg_path=None, date_created=""
):
    target = Path(jpeg_path)
    if not target.is_file() or target.stat().st_size <= 0:
        raise ValueError(f"Metadata verification failed: file is missing or empty: {target}")

    with Image.open(target) as verified:
        verified.verify()
    with Image.open(target) as image:
        width, height = image.size
        image.load()
    if width <= 0 or height <= 0:
        raise ValueError(f"Metadata verification failed: invalid image dimensions: {target}")

    exif = piexif.load(str(target))
    zero = exif.get("0th", {})
    if _decode_exif_text(zero.get(0x9C9B), "utf-16le") != title:
        raise ValueError("Metadata verification failed: title is missing")
    if description and _decode_exif_text(zero.get(piexif.ImageIFD.ImageDescription)) != description:
        raise ValueError("Metadata verification failed: description is missing")
    if keywords and _decode_exif_text(zero.get(0x9C9D), "utf-16le") != ";".join(keywords):
        raise ValueError("Metadata verification failed: keywords are missing")
    if creator:
        if _decode_exif_text(zero.get(piexif.ImageIFD.Artist)) != creator:
            raise ValueError("Metadata verification failed: creator is missing")

    raw = target.read_bytes()
    xmp_payloads = _jpeg_payloads(raw, 0xE1, _JPEG_XMP_HEADER)
    if len(xmp_payloads) != 1:
        raise ValueError("Metadata verification failed: XMP segment is missing")
    xmp_root = _xmp_root_from_payload(xmp_payloads[0])
    if _xmp_alt_text(xmp_root, "title") != title:
        raise ValueError("Metadata verification failed: XMP title is missing")
    if description and _xmp_alt_text(xmp_root, "description") != description:
        raise ValueError("Metadata verification failed: XMP description is missing")
    if keywords and _xmp_keyword_values(xmp_root) != keywords:
        raise ValueError("Metadata verification failed: XMP keywords are missing")

    iptc_records = _iptc_records_from_jpeg(raw)
    expected_iptc = {(2, 5): [title.encode("utf-8")[:64]]}
    if keywords:
        expected_iptc[(2, 25)] = [keyword.strip().encode("utf-8")[:64] for keyword in keywords if keyword.strip()]
    if description:
        expected_iptc[(2, 120)] = [description.encode("utf-8")[:2000]]
    if creator:
        expected_iptc[(2, 80)] = [creator.encode("utf-8")[:32]]
    date_meta = _parse_metadata_date(date_created)
    if date_meta:
        expected_iptc[(2, 55)] = [date_meta["iptc_date"].encode("ascii")]
    for key, expected_values in expected_iptc.items():
        actual_values = [
            entry["value"] for entry in iptc_records
            if (entry["record"], entry["dataset"]) == key
        ]
        if actual_values != expected_values:
            raise ValueError(f"Metadata verification failed: IPTC {key!r} is missing")
    if not iptc_records:
        raise ValueError("Metadata verification failed: XMP/IPTC segments are missing")
    if source_jpeg_path:
        _verify_jpeg_preservation(
            source_jpeg_path,
            target,
            description=description,
            keywords=keywords,
            creator=creator,
            date_created=date_created,
        )
    return {"path": str(target), "size": target.stat().st_size, "width": width, "height": height}


def _parse_metadata_date(date_created=""):
    raw = str(date_created or "").strip()
    if not raw:
        return {}
    parsed = None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return {}
    return {
        "input": raw,
        "display": parsed.strftime("%m/%d/%Y"),
        "xmp_date": parsed.strftime("%Y-%m-%d"),
        "xmp_datetime": parsed.strftime("%Y-%m-%dT00:00:00"),
        "iptc_date": parsed.strftime("%Y%m%d"),
        "exif_datetime": parsed.strftime("%Y:%m:%d 00:00:00"),
    }


def _build_xmp_segment(title, description, keywords, creator="", country="", date_created=""):
    """Build XMP APP1 segment bytes (FF E1 + length + payload)."""
    t, d, cr = _escape_xml(title), _escape_xml(description), _escape_xml(creator)
    country_xmp = _escape_xml(country.strip()) if country else ""
    date_meta = _parse_metadata_date(date_created)
    kw_items = "\n".join(f"      <rdf:li>{_escape_xml(k)}</rdf:li>" for k in keywords)

    xmp_body = f'''<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="PixTagra">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/"
    xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"
    photoshop:Headline="{t}">
   <dc:title>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{t}</rdf:li>
    </rdf:Alt>
   </dc:title>
   <dc:description>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{d}</rdf:li>
    </rdf:Alt>
   </dc:description>
   <dc:subject>
    <rdf:Bag>
{kw_items}
    </rdf:Bag>
   </dc:subject>
   <dc:creator>
    <rdf:Seq>
     <rdf:li>{cr}</rdf:li>
    </rdf:Seq>
   </dc:creator>
   <dc:rights>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">(c) {cr}</rdf:li>
    </rdf:Alt>
   </dc:rights>
 {f'   <photoshop:DateCreated>{date_meta["xmp_date"]}</photoshop:DateCreated>' if date_meta else ''}
 {f'   <xmp:CreateDate>{date_meta["xmp_datetime"]}</xmp:CreateDate>' if date_meta else ''}
 {f'   <xmp:MetadataDate>{date_meta["xmp_datetime"]}</xmp:MetadataDate>' if date_meta else ''}
 {f'   <Iptc4xmpCore:CountryName>{country_xmp}</Iptc4xmpCore:CountryName>' if country_xmp else ''}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>'''

    padding = " " * 2048 + "\n"
    xmp = f'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n{xmp_body}\n{padding}<?xpacket end="w"?>'
    ns_header = b"http://ns.adobe.com/xap/1.0/\x00"
    payload = ns_header + xmp.encode("utf-8")
    seg_len = len(payload) + 2
    return b"\xff\xe1" + seg_len.to_bytes(2, "big") + payload


def _build_iptc_segment(title, description, keywords, creator="", country="", date_created=""):
    """Build IPTC APP13 segment bytes (FF ED + length + payload)."""
    date_meta = _parse_metadata_date(date_created)

    def iptc_rec(record, dataset, value_bytes):
        return b"\x1c" + bytes([record, dataset]) + len(value_bytes).to_bytes(2, "big") + value_bytes

    iptc = bytearray()
    iptc += iptc_rec(1, 90, b"\x1b\x25\x47")

    if title:
        iptc += iptc_rec(2, 5, title.encode("utf-8")[:64])

    for kw in keywords:
        kw = kw.strip()
        if kw:
            iptc += iptc_rec(2, 25, kw.encode("utf-8")[:64])

    if description:
        iptc += iptc_rec(2, 120, description.encode("utf-8")[:2000])

    if date_meta:
        iptc += iptc_rec(2, 55, date_meta["iptc_date"].encode("ascii"))

    if country:
        iptc += iptc_rec(2, 101, country.strip().encode("utf-8")[:64])

    if creator:
        iptc += iptc_rec(2, 80, creator.encode("utf-8")[:32])
        iptc += iptc_rec(2, 116, f"(c) {creator}".encode("utf-8")[:128])

    iptc_bytes = bytes(iptc)
    pad = b"\x00" if len(iptc_bytes) % 2 else b""
    bim_block = (
        b"8BIM"
        + b"\x04\x04"
        + b"\x00\x00"
        + len(iptc_bytes).to_bytes(4, "big")
        + iptc_bytes + pad
    )

    payload = b"Photoshop 3.0\x00" + bim_block
    seg_len = len(payload) + 2
    return b"\xff\xed" + seg_len.to_bytes(2, "big") + payload


def _inject_metadata_rebuild(jpeg_path, title, description, keywords, creator="", country="", date_created=""):
    """Legacy JPEG rebuild path retained only for current non-JPEG conversion output.

    Reads the file ONCE, strips old XMP/IPTC, rebuilds with correct segment order:
      SOI → APP0(JFIF) → APP1(EXIF) → APP1(XMP) → APP13(IPTC) → rest → SOS → image

    This ordering is critical — Freepik, Adobe, Shutterstock all expect it.
    """
    with open(jpeg_path, "rb") as f:
        data = f.read()
    if data[:2] != b"\xff\xd8":
        raise ValueError(f"Metadata injection requires a JPEG file: {jpeg_path}")

    # Parse all JPEG segments into buckets
    jfif_segs = []     # APP0 (FF E0)
    exif_segs = []     # APP1-EXIF (FF E1 starting with "Exif\x00")
    other_segs = []    # Everything else we want to keep

    pos = 2
    while pos < len(data) - 3:
        if data[pos] != 0xFF:
            # Non-marker data — rest of file (shouldn't happen before SOS)
            other_segs.append(data[pos:])
            break
        marker = data[pos + 1]

        # End of metadata markers
        if marker == 0xDA:  # SOS — start of scan (image data follows)
            other_segs.append(data[pos:])
            break
        if marker == 0xD9:  # EOI
            other_segs.append(data[pos:])
            break

        # Restart markers (no length field)
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0x01):
            other_segs.append(data[pos:pos + 2])
            pos += 2
            continue

        seg_len = int.from_bytes(data[pos + 2:pos + 4], "big")
        seg_bytes = data[pos:pos + 2 + seg_len]  # Full segment including marker
        seg_data = data[pos + 4:pos + 2 + seg_len]  # Data only

        if marker == 0xE0:  # APP0 / JFIF
            jfif_segs.append(seg_bytes)
        elif marker == 0xE1:  # APP1 — could be EXIF or XMP
            is_xmp = seg_data.startswith(b"http://ns.adobe.com/xap/1.0/")
            if is_xmp:
                pass  # Strip old XMP — we'll inject new one
            else:
                exif_segs.append(seg_bytes)  # Keep EXIF
        elif marker == 0xED:  # APP13 / IPTC
            pass  # Strip old IPTC — we'll inject new one
        else:
            other_segs.append(seg_bytes)

        pos += 2 + seg_len

    # Build new XMP and IPTC segments
    xmp_seg = _build_xmp_segment(title, description, keywords, creator, country, date_created)
    iptc_seg = _build_iptc_segment(title, description, keywords, creator, country, date_created)

    # Reassemble in CORRECT order
    result = bytearray(b"\xff\xd8")       # SOI
    for s in jfif_segs:                     # APP0 / JFIF first
        result.extend(s)
    for s in exif_segs:                     # APP1 / EXIF second
        result.extend(s)
    result.extend(xmp_seg)                  # APP1 / XMP third
    result.extend(iptc_seg)                 # APP13 / IPTC fourth
    for s in other_segs:                    # SOS + image data last
        result.extend(s)
    return bytes(result)


def inject_metadata(
    jpeg_path, title, description, keywords, creator="", country="", date_created="", preserve_existing=True
):
    """Build a JPEG metadata copy without modifying jpeg_path.

    JPEG inputs use the preservation-first container merge. Existing non-JPEG
    save behavior reaches this function only after its historical JPEG
    conversion and explicitly requests the legacy rebuild route.
    """
    with open(jpeg_path, "rb") as source:
        data = source.read()
    if data[:2] != b"\xff\xd8":
        raise ValueError(f"Metadata injection requires a JPEG file: {jpeg_path}")
    if preserve_existing:
        # Location is intentionally ignored for JPEG until its field semantics
        # are approved. DateCreated is the only supported non-camera date write.
        return _merge_jpeg_metadata(data, title, description, keywords, creator, date_created)
    return _inject_metadata_rebuild(jpeg_path, title, description, keywords, creator, country, date_created)
