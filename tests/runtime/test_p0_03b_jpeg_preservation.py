"""P0-03B-1 JPEG preservation regressions using only synthetic temporary files."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
import xml.etree.ElementTree as ET

import piexif
import pytest
from PIL import Image, ImageCms

from tests.runtime.test_stage2_isolation import _WorkerHarness, _run_worker, app_module


XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
PHOTOSHOP_HEADER = b"Photoshop 3.0\x00"
ICC_HEADER = b"ICC_PROFILE\x00"
MANAGED_IPTC = {(2, 5), (2, 25), (2, 80), (2, 120), (2, 55)}
MANAGED_EXIF = {0x010E, 0x013B, 0x9C9B, 0x9C9D}
NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "xmpRights": "http://ns.adobe.com/xap/1.0/rights/",
    "cust": "urn:pixtagra:test:custom/",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _parse_jpeg(path: Path):
    raw = path.read_bytes()
    assert raw[:2] == b"\xff\xd8"
    pos = 2
    segments = []
    while pos < len(raw):
        assert raw[pos] == 0xFF
        marker = raw[pos + 1]
        if marker == 0xDA:
            return raw, segments, raw[pos:]
        length = int.from_bytes(raw[pos + 2:pos + 4], "big")
        assert length >= 2
        end = pos + 2 + length
        assert end <= len(raw)
        segments.append((marker, raw[pos:end], raw[pos + 4:end]))
        pos = end
    raise AssertionError("JPEG has no SOS marker")


def _replace_pre_scan_segments(path: Path, new_segments: list[bytes]):
    raw, _segments, scan = _parse_jpeg(path)
    path.write_bytes(raw[:2] + b"".join(new_segments) + scan)


def _scan_digest(path: Path) -> str:
    _raw, _segments, scan = _parse_jpeg(path)
    return hashlib.sha256(scan).hexdigest()


def _icc_segments(path: Path) -> list[bytes]:
    _raw, segments, _scan = _parse_jpeg(path)
    return [segment for marker, segment, payload in segments if marker == 0xE2 and payload.startswith(ICC_HEADER)]


def _unknown_app_segments(path: Path) -> list[bytes]:
    _raw, segments, _scan = _parse_jpeg(path)
    result = []
    for marker, segment, payload in segments:
        if marker == 0xE1 and (payload.startswith(b"Exif\x00\x00") or payload.startswith(XMP_HEADER)):
            continue
        if marker == 0xED and payload.startswith(PHOTOSHOP_HEADER):
            continue
        if marker == 0xE2 and payload.startswith(ICC_HEADER):
            continue
        if marker in range(0xE0, 0xF0):
            result.append(segment)
    return result


def _find_single_payload(path: Path, marker: int, prefix: bytes) -> bytes | None:
    _raw, segments, _scan = _parse_jpeg(path)
    matches = [payload for found_marker, _segment, payload in segments if found_marker == marker and payload.startswith(prefix)]
    assert len(matches) <= 1
    return matches[0] if matches else None


def _type_size(tag_type: int) -> int:
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
    return sizes[tag_type]


def _tiff_layout(tiff: bytes):
    order = tiff[:2]
    assert order in (b"II", b"MM")
    endian = "<" if order == b"II" else ">"
    assert struct.unpack_from(endian + "H", tiff, 2)[0] == 42
    offset = struct.unpack_from(endian + "I", tiff, 4)[0]
    assert 8 <= offset <= len(tiff) - 2
    count = struct.unpack_from(endian + "H", tiff, offset)[0]
    end = offset + 2 + count * 12 + 4
    assert end <= len(tiff)
    entries = []
    for index in range(count):
        entry_offset = offset + 2 + index * 12
        tag, tag_type, value_count = struct.unpack_from(endian + "HHI", tiff, entry_offset)
        raw_value = tiff[entry_offset + 8:entry_offset + 12]
        size = _type_size(tag_type) * value_count
        if size <= 4:
            value = raw_value[:size]
        else:
            value_offset = struct.unpack(endian + "I", raw_value)[0]
            assert value_offset + size <= len(tiff)
            value = tiff[value_offset:value_offset + size]
        entries.append((tag, tag_type, value_count, raw_value, value))
    return endian, offset, entries


def _exif_tiff(path: Path) -> bytes:
    payload = _find_single_payload(path, 0xE1, b"Exif\x00\x00")
    assert payload is not None
    return payload[6:]


def _ifd0_values(path: Path) -> dict[int, bytes]:
    _endian, _offset, entries = _tiff_layout(_exif_tiff(path))
    return {tag: value for tag, _kind, _count, _raw_value, value in entries}


def _add_unknown_ifd0_tag(path: Path, tag=0xC7A1, value=b"UnknownExifValue"):
    raw, segments, scan = _parse_jpeg(path)
    output = []
    replaced = False
    for marker, _segment, payload in segments:
        if marker != 0xE1 or not payload.startswith(b"Exif\x00\x00"):
            output.append(_segment)
            continue
        tiff = payload[6:]
        endian, _offset, entries = _tiff_layout(tiff)
        rebuilt = bytearray(tiff)
        data_offset = len(rebuilt)
        rebuilt.extend(value)
        if len(rebuilt) % 2:
            rebuilt.append(0)
        new_ifd_offset = len(rebuilt)
        all_entries = [(entry[0], entry[1], entry[2], entry[3]) for entry in entries]
        all_entries.append((tag, 7, len(value), struct.pack(endian + "I", data_offset)))
        all_entries.sort(key=lambda item: item[0])
        rebuilt.extend(struct.pack(endian + "H", len(all_entries)))
        for item_tag, tag_type, count, raw_value in all_entries:
            if item_tag == tag:
                raw_value = struct.pack(endian + "I", data_offset)
            rebuilt.extend(struct.pack(endian + "HHI", item_tag, tag_type, count))
            rebuilt.extend(raw_value)
        rebuilt.extend(struct.pack(endian + "I", 0))
        rebuilt[4:8] = struct.pack(endian + "I", new_ifd_offset)
        output.append(_jpeg_segment(0xE1, b"Exif\x00\x00" + bytes(rebuilt)))
        replaced = True
    assert replaced
    path.write_bytes(raw[:2] + b"".join(output) + scan)


def _iptc_record(record: int, dataset: int, value: bytes) -> bytes:
    return b"\x1c" + bytes([record, dataset]) + len(value).to_bytes(2, "big") + value


def _resource(resource_id: int, data: bytes, name=b"") -> bytes:
    name = name[:255]
    name_field = bytes([len(name)]) + name
    if len(name_field) % 2:
        name_field += b"\x00"
    padding = b"\x00" if len(data) % 2 else b""
    return b"8BIM" + resource_id.to_bytes(2, "big") + name_field + len(data).to_bytes(4, "big") + data + padding


def _parse_iptc_records(path: Path) -> list[tuple[int, int, bytes]]:
    payload = _find_single_payload(path, 0xED, PHOTOSHOP_HEADER)
    assert payload is not None
    data = payload[len(PHOTOSHOP_HEADER):]
    pos = 0
    iptc = None
    while pos < len(data):
        assert data[pos:pos + 4] == b"8BIM"
        resource_id = int.from_bytes(data[pos + 4:pos + 6], "big")
        name_len = data[pos + 6]
        name_end = pos + 7 + name_len
        if (1 + name_len) % 2:
            name_end += 1
        length = int.from_bytes(data[name_end:name_end + 4], "big")
        payload_start = name_end + 4
        payload_end = payload_start + length
        resource_data = data[payload_start:payload_end]
        pos = payload_end + (length % 2)
        if resource_id == 0x0404:
            assert iptc is None
            iptc = resource_data
    assert iptc is not None
    records = []
    pos = 0
    while pos < len(iptc):
        assert iptc[pos] == 0x1C
        record, dataset = iptc[pos + 1], iptc[pos + 2]
        length = int.from_bytes(iptc[pos + 3:pos + 5], "big")
        end = pos + 5 + length
        records.append((record, dataset, iptc[pos + 5:end]))
        pos = end
    return records


def _xmp_root(path: Path) -> ET.Element:
    payload = _find_single_payload(path, 0xE1, XMP_HEADER)
    assert payload is not None
    return ET.fromstring(payload[len(XMP_HEADER):])


def _xmp_text(path: Path, name: str) -> str:
    root = _xmp_root(path)
    element = root.find(f".//{name}", NS)
    return element.text if element is not None else ""


def _xmp_alt_value(path: Path, field: str, lang="x-default") -> str:
    root = _xmp_root(path)
    values = root.findall(f".//{field}/rdf:Alt/rdf:li", NS)
    for value in values:
        if value.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") == lang:
            return value.text or ""
    return ""


def _make_full_jpeg(path: Path, module, *, mode="RGB"):
    color = (20, 40, 60) if mode == "RGB" else 120
    image = Image.new(mode, (18, 14), color)
    exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif["0th"][module.piexif.ImageIFD.Make] = b"Synthetic Camera"
    exif["0th"][module.piexif.ImageIFD.Model] = b"Synthetic Model"
    exif["0th"][module.piexif.ImageIFD.Orientation] = 6
    exif["0th"][module.piexif.ImageIFD.Artist] = b"Legacy Creator"
    exif["0th"][module.piexif.ImageIFD.Copyright] = b"Legacy Rights Statement"
    exif["Exif"][module.piexif.ExifIFD.ExposureTime] = (1, 125)
    exif["Exif"][module.piexif.ExifIFD.FNumber] = (56, 10)
    exif["Exif"][module.piexif.ExifIFD.DateTimeOriginal] = b"2010:01:02 03:04:05"
    exif["Exif"][module.piexif.ExifIFD.LensModel] = b"Synthetic Lens 50mm"
    exif["GPS"][module.piexif.GPSIFD.GPSLatitudeRef] = b"N"
    exif["GPS"][module.piexif.GPSIFD.GPSLatitude] = ((35, 1), (0, 1), (0, 1))
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    image.save(path, "JPEG", quality=100, subsampling=0, exif=module.piexif.dump(exif), icc_profile=profile)
    image.close()

    raw, segments, scan = _parse_jpeg(path)
    rebuilt = []
    for marker, segment, payload in segments:
        if marker == 0xE2 and payload.startswith(ICC_HEADER):
            profile_bytes = payload[len(ICC_HEADER) + 2:]
            midpoint = len(profile_bytes) // 2
            rebuilt.append(_jpeg_segment(0xE2, ICC_HEADER + b"\x01\x02" + profile_bytes[:midpoint]))
            rebuilt.append(_jpeg_segment(0xE2, ICC_HEADER + b"\x02\x02" + profile_bytes[midpoint:]))
        else:
            rebuilt.append(segment)

    xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="' + NS["rdf"].encode() + b'" '
        b'xmlns:dc="' + NS["dc"].encode() + b'" xmlns:photoshop="' + NS["photoshop"].encode() + b'" '
        b'xmlns:xmpRights="' + NS["xmpRights"].encode() + b'" xmlns:cust="' + NS["cust"].encode() + b'">'
        b'<rdf:RDF><rdf:Description cust:Marker="KeepCustomXmp" cust:Extra="KeepExtra">'
        b'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Legacy Title</rdf:li>'
        b'<rdf:li xml:lang="fr-FR">Titre Heritage</rdf:li></rdf:Alt></dc:title>'
        b'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">Legacy Description</rdf:li>'
        b'<rdf:li xml:lang="fr-FR">Description Heritage</rdf:li></rdf:Alt></dc:description>'
        b'<dc:subject><rdf:Bag><rdf:li>legacy-keyword</rdf:li></rdf:Bag></dc:subject>'
        b'<dc:creator><rdf:Seq><rdf:li>Legacy Creator</rdf:li><rdf:li>Alias Creator</rdf:li></rdf:Seq></dc:creator>'
        b'<photoshop:Headline>Legacy Headline</photoshop:Headline>'
        b'<xmpRights:UsageTerms>Legacy Rights Statement</xmpRights:UsageTerms>'
        b'<cust:Preserve>Custom Namespace Value</cust:Preserve>'
        b'</rdf:Description></rdf:RDF></x:xmpmeta>'
    )
    iptc = b"".join(
        [
            _iptc_record(1, 90, b"\x1b%G"),
            _iptc_record(2, 5, b"Legacy Title"),
            _iptc_record(2, 25, b"legacy-keyword"),
            _iptc_record(2, 120, b"Legacy Description"),
            _iptc_record(2, 80, b"Legacy Creator"),
            _iptc_record(2, 116, b"Legacy Rights Statement"),
            _iptc_record(2, 55, b"20100102"),
            _iptc_record(2, 90, b"Legacy City"),
            _iptc_record(2, 101, b"Legacy Country"),
            _iptc_record(2, 200, b"KeepUnknownIptc"),
        ]
    )
    rebuilt.extend(
        [
            _jpeg_segment(0xE1, XMP_HEADER + xmp),
            _jpeg_segment(0xED, PHOTOSHOP_HEADER + _resource(0x0404, iptc) + _resource(0x040F, b"KeepNonIptcResource")),
            _jpeg_segment(0xE3, b"UnknownAppSegmentPayload"),
        ]
    )
    path.write_bytes(raw[:2] + b"".join(rebuilt) + scan)
    _add_unknown_ifd0_tag(path)


def _make_photoshop_resource_only_jpeg(path: Path, module):
    """Create a JPEG with APP13 data but no pre-existing IPTC resource."""
    _make_full_jpeg(path, module)
    raw, segments, scan = _parse_jpeg(path)
    rebuilt = []
    for marker, segment, payload in segments:
        if marker == 0xED and payload.startswith(PHOTOSHOP_HEADER):
            rebuilt.append(
                _jpeg_segment(
                    0xED,
                    PHOTOSHOP_HEADER + _resource(0x040F, b"KeepNonIptcResource"),
                )
            )
        else:
            rebuilt.append(segment)
    path.write_bytes(raw[:2] + b"".join(rebuilt) + scan)


def _make_malformed_metadata_jpeg(path: Path, module, kind: str):
    _make_full_jpeg(path, module)
    raw, segments, scan = _parse_jpeg(path)
    rebuilt = []
    for marker, segment, payload in segments:
        if kind == "xmp" and marker == 0xE1 and payload.startswith(XMP_HEADER):
            rebuilt.append(_jpeg_segment(0xE1, XMP_HEADER + b"<not-valid-xmp"))
        elif kind == "iptc" and marker == 0xED and payload.startswith(PHOTOSHOP_HEADER):
            rebuilt.append(_jpeg_segment(0xED, PHOTOSHOP_HEADER + b"not-a-resource"))
        elif kind == "exif" and marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            rebuilt.append(_jpeg_segment(0xE1, b"Exif\x00\x00II*\x00\xff\xff\xff\x7f"))
        else:
            rebuilt.append(segment)
    path.write_bytes(raw[:2] + b"".join(rebuilt) + scan)


def _item(source: Path, title="New Managed Title"):
    return {
        "card": object(),
        "path": str(source),
        "title": title,
        "description": "New managed description.",
        "keywords": ["new", "managed", "keywords"],
    }


def _saved_output(result) -> Path:
    assert result["ok"] == 1
    assert result["errors"] == []
    assert len(result["saved_paths"]) == 1
    return Path(result["saved_paths"][0])


def _run_worker_with_options(module, items, output_dir, creator="", country="", date_created=""):
    harness = _WorkerHarness()
    module.App._save_worker(
        harness,
        items,
        output_dir=output_dir,
        creator=creator,
        country=country,
        date_created=date_created,
    )
    assert harness.done is not None
    return harness.done


def _assert_common_preservation(source: Path, output: Path, module):
    assert output.suffix.lower() == ".jpg"
    assert output.read_bytes()[:2] == b"\xff\xd8"
    assert _scan_digest(output) == _scan_digest(source)
    assert _icc_segments(output) == _icc_segments(source)
    assert _unknown_app_segments(output) == _unknown_app_segments(source)
    source_exif = module.piexif.load(str(source))
    output_exif = module.piexif.load(str(output))
    for ifd_name, tag in (
        ("0th", module.piexif.ImageIFD.Orientation),
        ("0th", module.piexif.ImageIFD.Make),
        ("0th", module.piexif.ImageIFD.Model),
        ("Exif", module.piexif.ExifIFD.ExposureTime),
        ("Exif", module.piexif.ExifIFD.FNumber),
        ("Exif", module.piexif.ExifIFD.LensModel),
        ("Exif", module.piexif.ExifIFD.DateTimeOriginal),
        ("GPS", module.piexif.GPSIFD.GPSLatitude),
    ):
        assert output_exif[ifd_name][tag] == source_exif[ifd_name][tag]
    assert _ifd0_values(output)[0xC7A1] == _ifd0_values(source)[0xC7A1]
    assert _xmp_text(output, "cust:Preserve") == "Custom Namespace Value"
    assert _xmp_text(output, "photoshop:Headline") == "Legacy Headline"
    assert _xmp_text(output, "xmpRights:UsageTerms") == "Legacy Rights Statement"
    source_iptc = _parse_iptc_records(source)
    output_iptc = _parse_iptc_records(output)
    assert [record for record in output_iptc if record[:2] not in MANAGED_IPTC] == [record for record in source_iptc if record[:2] not in MANAGED_IPTC]
    assert b"KeepNonIptcResource" in output.read_bytes()


def test_jpeg_container_merge_preserves_scan_icc_exif_xmp_iptc_and_unknown_segments(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(source, app_module)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source)], output_dir=output_dir, creator="")
    output = _saved_output(result)

    assert _sha256(source) == source_hash
    _assert_common_preservation(source, output, app_module)
    assert _xmp_alt_value(output, "dc:title") == "New Managed Title"
    assert _xmp_alt_value(output, "dc:title", "fr-FR") == "Titre Heritage"
    assert _xmp_alt_value(output, "dc:description") == "New managed description."
    assert _xmp_alt_value(output, "dc:description", "fr-FR") == "Description Heritage"
    assert [item.text for item in _xmp_root(output).findall(".//dc:subject/rdf:Bag/rdf:li", NS)] == ["new", "managed", "keywords"]


def test_jpeg_with_photoshop_resource_but_no_iptc_saves_without_false_rejection(app_module, tmp_path):
    source = tmp_path / "photoshop-resource-only.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_photoshop_resource_only_jpeg(source, app_module)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source)], output_dir=output_dir, creator="")
    output = _saved_output(result)

    assert _sha256(source) == source_hash
    assert _scan_digest(output) == _scan_digest(source)
    assert _icc_segments(output) == _icc_segments(source)
    assert _unknown_app_segments(output) == _unknown_app_segments(source)
    assert b"KeepNonIptcResource" in output.read_bytes()
    assert _xmp_alt_value(output, "dc:title") == "New Managed Title"
    assert (2, 5, b"New Managed Title") in _parse_iptc_records(output)


def test_jpeg_without_photoshop_app13_saves_without_false_rejection(app_module, tmp_path):
    source = tmp_path / "plain-source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    image = Image.new("RGB", (18, 14), (20, 40, 60))
    image.save(source, "JPEG", quality=100, subsampling=0)
    image.close()
    source_hash = _sha256(source)

    output = _saved_output(_run_worker(app_module, [_item(source)], output_dir=output_dir, creator=""))

    assert _sha256(source) == source_hash
    assert _scan_digest(output) == _scan_digest(source)
    assert _xmp_alt_value(output, "dc:title") == "New Managed Title"
    assert (2, 5, b"New Managed Title") in _parse_iptc_records(output)


def test_blank_creator_and_existing_rights_are_preserved(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(source, app_module)

    output = _saved_output(_run_worker(app_module, [_item(source)], output_dir=output_dir, creator=""))
    exif = app_module.piexif.load(str(output))

    assert exif["0th"][app_module.piexif.ImageIFD.Artist] == b"Legacy Creator"
    assert exif["0th"][app_module.piexif.ImageIFD.Copyright] == b"Legacy Rights Statement"
    assert _xmp_text(output, "xmpRights:UsageTerms") == "Legacy Rights Statement"
    assert b"(c) Legacy Creator" not in output.read_bytes()


def test_blank_optional_fields_preserve_existing_creator_dates_description_and_keywords(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(source, app_module)
    item = _item(source)
    item["description"] = ""
    item["keywords"] = []

    output = _saved_output(
        _run_worker_with_options(
            app_module,
            [item],
            output_dir,
            creator="",
            country="",
            date_created="",
        )
    )
    records = _parse_iptc_records(output)

    assert _xmp_alt_value(output, "dc:description") == "Legacy Description"
    assert [entry.text for entry in _xmp_root(output).findall(".//dc:subject/rdf:Bag/rdf:li", NS)] == ["legacy-keyword"]
    assert (2, 80, b"Legacy Creator") in records
    assert (2, 55, b"20100102") in records
    assert (2, 120, b"Legacy Description") in records
    assert (2, 25, b"legacy-keyword") in records


def test_explicit_creator_updates_creator_but_preserves_rights_and_aliases(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(source, app_module)

    output = _saved_output(_run_worker(app_module, [_item(source)], output_dir=output_dir, creator="New Creator"))
    exif = app_module.piexif.load(str(output))
    creators = [item.text for item in _xmp_root(output).findall(".//dc:creator/rdf:Seq/rdf:li", NS)]

    assert exif["0th"][app_module.piexif.ImageIFD.Artist] == b"New Creator"
    assert exif["0th"][app_module.piexif.ImageIFD.Copyright] == b"Legacy Rights Statement"
    assert creators == ["New Creator", "Alias Creator"]
    assert _xmp_text(output, "xmpRights:UsageTerms") == "Legacy Rights Statement"
    assert b"(c) New Creator" not in output.read_bytes()


def test_shoot_date_does_not_change_datetimeoriginal_or_blank_dates(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(source, app_module)
    original = app_module.piexif.load(str(source))["Exif"][app_module.piexif.ExifIFD.DateTimeOriginal]

    output = _saved_output(
        _run_worker(
            app_module,
            [_item(source)],
            output_dir=output_dir,
            creator="",
        )
    )
    assert app_module.piexif.load(str(output))["Exif"][app_module.piexif.ExifIFD.DateTimeOriginal] == original
    assert _xmp_text(output, "photoshop:DateCreated") == "2026-08-02"
    assert (2, 55, b"20260802") in _parse_iptc_records(output)


def test_location_and_country_are_not_overwritten_by_current_location_input(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(source, app_module)
    item = _item(source)

    harness = type("Harness", (), {"done": None, "after": lambda self, _delay, callback: callback(), "_save_done": lambda self, ok, cards, out, errors, paths=None, warnings=None: setattr(self, "done", {"ok": ok, "errors": list(errors), "saved_paths": list(paths or [])})})()
    app_module.App._save_worker(harness, [item], output_dir=output_dir, creator="", country="Incorrect Replacement Country", date_created="08/02/2026")
    assert harness.done["ok"] == 1
    output = Path(harness.done["saved_paths"][0])

    records = _parse_iptc_records(output)
    assert (2, 101, b"Legacy Country") in records
    assert b"Incorrect Replacement Country" not in output.read_bytes()
    assert app_module.piexif.load(str(output))["Exif"][app_module.piexif.ExifIFD.DateTimeOriginal] == b"2010:01:02 03:04:05"


@pytest.mark.parametrize("mode", ["RGB", "CMYK", "L"])
def test_jpeg_modes_do_not_reencode_scan_data(app_module, tmp_path, mode):
    source = tmp_path / f"{mode}.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(source, app_module, mode=mode)

    output = _saved_output(_run_worker(app_module, [_item(source, f"{mode} managed title")], output_dir=output_dir, creator=""))

    assert _scan_digest(output) == _scan_digest(source)
    with Image.open(source) as before, Image.open(output) as after:
        assert after.mode == before.mode


@pytest.mark.parametrize("kind", ["exif", "xmp", "iptc"])
def test_malformed_managed_metadata_blocks_save_without_publishing(app_module, tmp_path, kind):
    source = tmp_path / f"malformed-{kind}.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_malformed_metadata_jpeg(source, app_module, kind)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source)], output_dir=output_dir, creator="")

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert result["errors"]
    assert source.name in result["errors"][0]
    assert str(source) not in result["errors"][0]
    assert _sha256(source) == source_hash
    assert not list(output_dir.glob("*.jpg"))
    assert not list(output_dir.glob(".pixtagra_*.tmp"))


def test_jpeg_merge_exception_cleans_staging_and_keeps_card_unsaved(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(source, app_module)
    source_hash = _sha256(source)
    monkeypatch.setattr(
        app_module.metadata,
        "_merge_jpeg_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("synthetic JPEG merge failure")),
    )

    result = _run_worker(app_module, [_item(source)], output_dir=output_dir, creator="")

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert "synthetic JPEG merge failure" in result["errors"][0]
    assert _sha256(source) == source_hash
    assert not list(output_dir.glob("*.jpg"))
    assert not list(output_dir.glob(".pixtagra_*.tmp"))


def test_jpeg_readback_failure_blocks_publication_and_cleans_staging(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(source, app_module)
    source_hash = _sha256(source)
    monkeypatch.setattr(
        app_module.metadata,
        "_verify_written_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("synthetic JPEG readback failure")),
    )

    result = _run_worker(app_module, [_item(source)], output_dir=output_dir, creator="")

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert "synthetic JPEG readback failure" in result["errors"][0]
    assert _sha256(source) == source_hash
    assert not list(output_dir.glob("*.jpg"))
    assert not list(output_dir.glob(".pixtagra_*.tmp"))


def test_default_save_and_existing_target_keep_source_and_existing_output(app_module, tmp_path):
    source = tmp_path / "New_Managed_Title.jpg"
    _make_full_jpeg(source, app_module)
    source_hash = _sha256(source)
    existing = tmp_path / "New_Managed_Title_1.jpg"
    existing.write_bytes(b"existing target")

    output = _saved_output(_run_worker(app_module, [_item(source)], output_dir=None, creator=""))

    assert _sha256(source) == source_hash
    assert existing.read_bytes() == b"existing target"
    assert output.name == "New_Managed_Title_2.jpg"


def test_concurrent_same_title_outputs_remain_distinct(app_module, tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_full_jpeg(first, app_module)
    _make_full_jpeg(second, app_module)

    result = _run_worker(
        app_module,
        [_item(first, "Shared Title"), _item(second, "Shared Title")],
        output_dir=output_dir,
        creator="",
    )

    assert result["ok"] == 2
    assert len(set(result["saved_paths"])) == 2
    assert all(Path(path).exists() for path in result["saved_paths"])
