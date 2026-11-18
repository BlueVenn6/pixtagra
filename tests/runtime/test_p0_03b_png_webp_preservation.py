from __future__ import annotations

import hashlib
import struct
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageCms
import piexif
import pytest

from tests.runtime.test_stage2_isolation import _WorkerHarness, app_module


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
XMP_KEYWORD = b"XML:com.adobe.xmp"
XMP_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xmpRights": "http://ns.adobe.com/xap/1.0/rights/",
    "Iptc4xmpCore": "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _icc_profile() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _item(source: Path, title: str, description="PNG/WebP preservation fixture.", keywords=None, card=None):
    return {
        "card": card if card is not None else object(),
        "path": str(source),
        "title": title,
        "description": description,
        "keywords": list(keywords if keywords is not None else ["preserve", "metadata", "fixture"]),
    }


def _run_worker(module, items, output_dir=None, creator="Current Creator", country="Incorrect Replacement Country", date="2026-08-02"):
    harness = _WorkerHarness()
    module.App._save_worker(
        harness,
        items,
        output_dir=output_dir,
        creator=creator,
        country=country,
        date_created=date,
    )
    assert harness.done is not None
    return harness.done


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(4, "big")
        + kind
        + payload
        + (zlib.crc32(kind + payload) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def _png_chunks(data: bytes, *, validate=True):
    assert data.startswith(PNG_SIGNATURE)
    chunks = []
    pos = len(PNG_SIGNATURE)
    while pos < len(data):
        assert pos + 12 <= len(data)
        length = int.from_bytes(data[pos:pos + 4], "big")
        end = pos + 12 + length
        assert end <= len(data)
        kind = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        crc = int.from_bytes(data[pos + 8 + length:end], "big")
        if validate:
            assert crc == (zlib.crc32(kind + payload) & 0xFFFFFFFF)
        chunks.append((kind, payload, data[pos:end]))
        pos = end
    assert pos == len(data)
    return chunks


def _png_itxt(keyword: bytes, text: bytes, *, compressed=False) -> bytes:
    encoded = zlib.compress(text) if compressed else text
    return keyword + b"\x00" + bytes([int(compressed), 0]) + b"\x00\x00" + encoded


def _parse_png_itxt(payload: bytes):
    keyword, remainder = payload.split(b"\x00", 1)
    assert len(remainder) >= 2
    compression_flag, compression_method = remainder[:2]
    assert compression_method == 0
    language, remainder = remainder[2:].split(b"\x00", 1)
    translated, text = remainder.split(b"\x00", 1)
    if compression_flag:
        text = zlib.decompress(text)
    return keyword, language, translated, text


def _insert_png_chunks(path: Path, extra):
    source = _png_chunks(path.read_bytes())
    output = bytearray(PNG_SIGNATURE)
    inserted = False
    for kind, _payload, raw in source:
        if kind == b"IDAT" and not inserted:
            output.extend(b"".join(extra))
            inserted = True
        output.extend(raw)
    assert inserted
    path.write_bytes(bytes(output))


def _xmp_packet() -> bytes:
    return (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        b'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/" '
        b'xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/" xmlns:cust="urn:fixture:custom">'
        b'<rdf:RDF><rdf:Description cust:Marker="KeepCustomXmp">'
        b'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Legacy Title</rdf:li>'
        b'<rdf:li xml:lang="fr-FR">Titre Heritage</rdf:li></rdf:Alt></dc:title>'
        b'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">Legacy Description</rdf:li></rdf:Alt></dc:description>'
        b'<dc:subject><rdf:Bag><rdf:li>legacy-keyword</rdf:li></rdf:Bag></dc:subject>'
        b'<dc:creator><rdf:Seq><rdf:li>Legacy Creator</rdf:li><rdf:li>Alias Creator</rdf:li></rdf:Seq></dc:creator>'
        b'<xmpRights:UsageTerms>Legacy Rights Statement</xmpRights:UsageTerms>'
        b'<Iptc4xmpCore:CountryName>Legacy Country</Iptc4xmpCore:CountryName>'
        b'<cust:Preserve>Custom Namespace Value</cust:Preserve>'
        b'</rdf:Description></rdf:RDF></x:xmpmeta>'
    )


def _legacy_exif():
    exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif["0th"][piexif.ImageIFD.Artist] = b"Legacy Creator"
    exif["0th"][piexif.ImageIFD.Copyright] = b"Legacy Rights Statement"
    exif["0th"][piexif.ImageIFD.ImageDescription] = b"Legacy Description"
    exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = b"2010:01:02 03:04:05"
    return piexif.dump(exif)


def _make_png(path: Path, mode="RGBA", *, include_metadata=True, include_icc=True, include_color_chunks=False):
    save_options = {"exif": _legacy_exif()}
    if include_icc:
        save_options["icc_profile"] = _icc_profile()
    if mode == "P":
        image = Image.new("P", (7, 5))
        image.putpalette([255, 0, 0, 0, 0, 255] + [0, 0, 0] * 254)
        image.putdata([(x + y) % 2 for y in range(5) for x in range(7)])
        image.save(path, "PNG", transparency=0, **save_options)
    else:
        image = Image.new(mode, (7, 5))
        if mode == "RGBA":
            image.putdata([(x * 20, y * 30, (x + y) * 11, (x * 37 + y * 29) % 256) for y in range(5) for x in range(7)])
        else:
            image.putdata([(x * 20, y * 30, (x + y) * 11) for y in range(5) for x in range(7)])
        image.save(path, "PNG", **save_options)
    image.close()
    if include_metadata:
        extra_chunks = [
            _png_chunk(b"tEXt", b"LegacyText\x00Keep text metadata"),
            _png_chunk(b"zTXt", b"LegacyZTxt\x00\x00" + zlib.compress(b"Keep compressed text metadata")),
            _png_chunk(b"iTXt", _png_itxt(b"LegacyITXt", b"Keep international text", compressed=True)),
            _png_chunk(b"iTXt", _png_itxt(XMP_KEYWORD, _xmp_packet())),
            _png_chunk(b"vpAg", b"Keep unknown ancillary payload"),
        ]
        if include_color_chunks:
            extra_chunks.extend(
                [
                    _png_chunk(b"sRGB", b"\x00"),
                    _png_chunk(b"gAMA", (45455).to_bytes(4, "big")),
                    _png_chunk(
                        b"cHRM",
                        b"".join(value.to_bytes(4, "big") for value in (31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000)),
                    ),
                ]
            )
        _insert_png_chunks(path, extra_chunks)


def _riff_chunk(kind: bytes, payload: bytes) -> bytes:
    return kind + len(payload).to_bytes(4, "little") + payload + (b"\x00" if len(payload) % 2 else b"")


def _webp_chunks(data: bytes, *, validate=True):
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if validate:
        assert len(data) == int.from_bytes(data[4:8], "little") + 8
    chunks = []
    pos = 12
    while pos < len(data):
        assert pos + 8 <= len(data)
        kind = data[pos:pos + 4]
        length = int.from_bytes(data[pos + 4:pos + 8], "little")
        payload_end = pos + 8 + length
        end = payload_end + (length % 2)
        assert end <= len(data)
        padding = data[payload_end:end]
        if validate and padding:
            assert padding == b"\x00"
        chunks.append((kind, data[pos + 8:payload_end], data[pos:end]))
        pos = end
    assert pos == len(data)
    return chunks


def _rebuild_webp(chunks):
    payload = b"".join(_riff_chunk(kind, data) for kind, data in chunks)
    return b"RIFF" + (len(payload) + 4).to_bytes(4, "little") + b"WEBP" + payload


def _make_webp(path: Path, *, lossless=True, alpha=True, include_metadata=True):
    mode = "RGBA" if alpha else "RGB"
    image = Image.new(mode, (7, 5))
    pixels = []
    for y in range(5):
        for x in range(7):
            rgb = (x * 20, y * 30, (x + y) * 11)
            pixels.append(rgb + ((x * 37 + y * 29) % 256,) if alpha else rgb)
    image.putdata(pixels)
    image.save(path, "WEBP", lossless=lossless, quality=100, icc_profile=_icc_profile(), exif=_legacy_exif())
    image.close()
    if include_metadata:
        chunks = [(kind, payload) for kind, payload, _raw in _webp_chunks(path.read_bytes())]
        assert chunks[0][0] == b"VP8X"
        vp8x = bytearray(chunks[0][1])
        vp8x[0] |= 0x04
        chunks[0] = (b"VP8X", bytes(vp8x))
        chunks.extend([(b"XMP ", _xmp_packet()), (b"UNKN", b"Keep unknown RIFF payload")])
        path.write_bytes(_rebuild_webp(chunks))


def _make_animated_webp(path: Path):
    first = Image.new("RGBA", (7, 5), (10, 20, 30, 40))
    second = Image.new("RGBA", (7, 5), (60, 70, 80, 90))
    first.save(path, "WEBP", save_all=True, append_images=[second], duration=100, loop=0, lossless=True)
    first.close()
    second.close()
    kinds = [kind for kind, _payload, _raw in _webp_chunks(path.read_bytes())]
    assert b"ANIM" in kinds and b"ANMF" in kinds


def _image_snapshot(path: Path):
    raw = path.read_bytes()
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        snapshot = {
            "format": image.format,
            "size": image.size,
            "mode": image.mode,
            "pixel": hashlib.sha256(rgba.tobytes()).hexdigest(),
            "alpha": hashlib.sha256(alpha.tobytes()).hexdigest(),
            "icc": hashlib.sha256(image.info.get("icc_profile", b"")).hexdigest(),
        }
        rgba.close()
        alpha.close()
    snapshot["sha256"] = hashlib.sha256(raw).hexdigest()
    return snapshot


def _png_payloads(path: Path, kind: bytes):
    return [payload for found, payload, _raw in _png_chunks(path.read_bytes()) if found == kind]


def _png_xmp(path: Path):
    matches = []
    for payload in _png_payloads(path, b"iTXt"):
        keyword, _language, _translated, text = _parse_png_itxt(payload)
        if keyword == XMP_KEYWORD:
            matches.append(text)
    assert len(matches) == 1
    return ET.fromstring(matches[0])


def _webp_payloads(path: Path, kind: bytes):
    return [payload for found, payload, _raw in _webp_chunks(path.read_bytes()) if found == kind]


def _xmp_value(root, path):
    node = root.find(path, XMP_NS)
    return "" if node is None else node.text or ""


def _xmp_alt(root, field, language="x-default"):
    for item in root.findall(f".//dc:{field}/rdf:Alt/rdf:li", XMP_NS):
        if item.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") == language:
            return item.text or ""
    return ""


def _exif_from_payload(payload: bytes):
    return piexif.load(b"Exif\x00\x00" + payload)


def _assert_common_metadata(source: Path, output: Path, *, container="png"):
    if container == "png":
        source_exif = _exif_from_payload(_png_payloads(source, b"eXIf")[0])
        output_exif = _exif_from_payload(_png_payloads(output, b"eXIf")[0])
        source_xmp = _png_xmp(source)
        output_xmp = _png_xmp(output)
    else:
        source_exif = _exif_from_payload(_webp_payloads(source, b"EXIF")[0])
        output_exif = _exif_from_payload(_webp_payloads(output, b"EXIF")[0])
        source_xmp = ET.fromstring(_webp_payloads(source, b"XMP ")[0])
        output_xmp = ET.fromstring(_webp_payloads(output, b"XMP ")[0])
    assert source_exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == output_exif["Exif"][piexif.ExifIFD.DateTimeOriginal]
    assert output_exif["0th"][piexif.ImageIFD.Copyright] == b"Legacy Rights Statement"
    assert _xmp_value(output_xmp, ".//xmpRights:UsageTerms") == "Legacy Rights Statement"
    assert _xmp_value(output_xmp, ".//Iptc4xmpCore:CountryName") == "Legacy Country"
    assert _xmp_value(output_xmp, ".//{urn:fixture:custom}Preserve") == "Custom Namespace Value"
    assert _xmp_alt(output_xmp, "title", "fr-FR") == "Titre Heritage"
    assert _xmp_value(source_xmp, ".//{urn:fixture:custom}Preserve") == _xmp_value(output_xmp, ".//{urn:fixture:custom}Preserve")


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "P"])
def test_png_default_save_preserves_format_pixels_alpha_and_idat(app_module, tmp_path, mode):
    source = tmp_path / f"source-{mode}.png"
    _make_png(source, mode=mode)
    source_hash = _sha256(source)
    before = _image_snapshot(source)
    idat_before = hashlib.sha256(b"".join(_png_payloads(source, b"IDAT"))).hexdigest()

    result = _run_worker(app_module, [_item(source, "PNG Managed Title")], output_dir=None)
    output = tmp_path / "PNG_Managed_Title.png"

    assert result["ok"] == 1
    assert result["saved_paths"] == [str(output)]
    assert _sha256(source) == source_hash
    assert output.suffix == ".png"
    assert _image_snapshot(output) == {**before, "sha256": _image_snapshot(output)["sha256"]}
    assert hashlib.sha256(b"".join(_png_payloads(output, b"IDAT"))).hexdigest() == idat_before


def test_png_preserves_icc_text_exif_xmp_and_unknown_ancillary_chunk(app_module, tmp_path):
    source = tmp_path / "source.png"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_png(source)
    before = _image_snapshot(source)
    protected = [raw for kind, _payload, raw in _png_chunks(source.read_bytes()) if kind not in {b"eXIf"} and not (kind == b"iTXt" and _parse_png_itxt(_payload)[0] == XMP_KEYWORD)]

    result = _run_worker(app_module, [_item(source, "PNG Managed Title")], output_dir=output_dir)
    output = output_dir / "PNG_Managed_Title.png"

    assert result["ok"] == 1
    assert _sha256(source) == before["sha256"]
    assert _image_snapshot(output)["pixel"] == before["pixel"]
    assert _image_snapshot(output)["alpha"] == before["alpha"]
    assert _image_snapshot(output)["icc"] == before["icc"]
    retained = [raw for kind, _payload, raw in _png_chunks(output.read_bytes()) if kind not in {b"eXIf"} and not (kind == b"iTXt" and _parse_png_itxt(_payload)[0] == XMP_KEYWORD)]
    assert retained == protected
    assert b"Keep text metadata" in output.read_bytes()
    assert b"Keep international text" not in output.read_bytes()
    assert _parse_png_itxt([payload for kind, payload, _raw in _png_chunks(output.read_bytes()) if kind == b"iTXt" and _parse_png_itxt(payload)[0] == b"LegacyITXt"][0])[3] == b"Keep international text"
    ztxt = [payload for kind, payload, _raw in _png_chunks(output.read_bytes()) if kind == b"zTXt" and payload.startswith(b"LegacyZTxt\x00")]
    assert zlib.decompress(ztxt[0].split(b"\x00", 2)[2]) == b"Keep compressed text metadata"
    assert b"Keep unknown ancillary payload" in output.read_bytes()
    output_xmp = _png_xmp(output)
    assert _xmp_alt(output_xmp, "title") == "PNG Managed Title"
    assert _xmp_alt(output_xmp, "description") == "PNG/WebP preservation fixture."
    assert [entry.text for entry in output_xmp.findall(".//dc:subject/rdf:Bag/rdf:li", XMP_NS)] == ["preserve", "metadata", "fixture"]
    assert _xmp_value(output_xmp, ".//dc:creator/rdf:Seq/rdf:li") == "Current Creator"
    assert _xmp_value(output_xmp, ".//{http://ns.adobe.com/photoshop/1.0/}DateCreated") == "2026-08-02"
    assert _exif_from_payload(_png_payloads(output, b"eXIf")[0])["0th"][piexif.ImageIFD.Artist] == b"Current Creator"
    _assert_common_metadata(source, output)


def test_png_blank_creator_keeps_identity_rights_dates_and_location(app_module, tmp_path):
    source = tmp_path / "source.png"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_png(source)

    result = _run_worker(
        app_module,
        [_item(source, "PNG Blank Creator", description="", keywords=[])],
        output_dir=output_dir,
        creator="",
    )
    output = output_dir / "PNG_Blank_Creator.png"
    output_exif = _exif_from_payload(_png_payloads(output, b"eXIf")[0])

    assert result["ok"] == 1
    assert output_exif["0th"][piexif.ImageIFD.Artist] == b"Legacy Creator"
    assert _xmp_value(_png_xmp(output), ".//dc:creator/rdf:Seq/rdf:li") == "Legacy Creator"
    _assert_common_metadata(source, output)


def test_png_preserves_srgb_gamma_and_chromaticity_chunks(app_module, tmp_path):
    source = tmp_path / "color.png"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_png(source, mode="RGB", include_icc=False, include_color_chunks=True)
    protected_color = [
        raw
        for kind, _payload, raw in _png_chunks(source.read_bytes())
        if kind in {b"sRGB", b"gAMA", b"cHRM"}
    ]

    result = _run_worker(app_module, [_item(source, "PNG Color Chunks")], output_dir=output_dir)
    output = output_dir / "PNG_Color_Chunks.png"

    assert result["ok"] == 1
    assert [raw for kind, _payload, raw in _png_chunks(output.read_bytes()) if kind in {b"sRGB", b"gAMA", b"cHRM"}] == protected_color


@pytest.mark.parametrize("mutation", ["crc", "length", "duplicate-ihdr"])
def test_malformed_png_blocks_save_without_final_output(app_module, tmp_path, mutation):
    source = tmp_path / f"bad-{mutation}.png"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_png(source)
    raw = bytearray(source.read_bytes())
    if mutation == "crc":
        raw[-1] ^= 0x01
    elif mutation == "length":
        raw[8:12] = (0x7FFFFFFF).to_bytes(4, "big")
    else:
        ihdr = _png_chunks(bytes(raw))[0][2]
        raw = bytearray(PNG_SIGNATURE + ihdr + bytes(raw[len(PNG_SIGNATURE):]))
    source.write_bytes(raw)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source, "Blocked PNG")], output_dir=output_dir)

    assert result["ok"] == 0
    assert result["saved_paths"] == []
    assert _sha256(source) == source_hash
    assert not list(output_dir.iterdir())


@pytest.mark.parametrize("lossless,alpha,expected_payload", [(False, False, b"VP8 "), (False, True, b"VP8 "), (True, True, b"VP8L")])
def test_static_webp_default_save_preserves_format_pixels_alpha_and_bitstream(app_module, tmp_path, lossless, alpha, expected_payload):
    source = tmp_path / f"source-{'lossless' if lossless else 'lossy'}.webp"
    _make_webp(source, lossless=lossless, alpha=alpha)
    source_hash = _sha256(source)
    before = _image_snapshot(source)
    payload_before = hashlib.sha256(b"".join(_webp_payloads(source, expected_payload))).hexdigest()

    result = _run_worker(app_module, [_item(source, "WebP Managed Title")], output_dir=None)
    output = tmp_path / "WebP_Managed_Title.webp"

    assert result["ok"] == 1
    assert result["saved_paths"] == [str(output)]
    assert _sha256(source) == source_hash
    assert output.suffix == ".webp"
    after = _image_snapshot(output)
    assert after["format"] == "WEBP"
    assert after["size"] == before["size"]
    assert after["pixel"] == before["pixel"]
    assert after["alpha"] == before["alpha"]
    assert hashlib.sha256(b"".join(_webp_payloads(output, expected_payload))).hexdigest() == payload_before
    assert _webp_payloads(output, b"ALPH") == _webp_payloads(source, b"ALPH")


def test_static_webp_preserves_iccp_exif_xmp_unknown_chunks_and_feature_flags(app_module, tmp_path):
    source = tmp_path / "source.webp"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_webp(source, lossless=True, alpha=True)
    before = _image_snapshot(source)
    protected = [raw for kind, _payload, raw in _webp_chunks(source.read_bytes()) if kind not in {b"VP8X", b"EXIF", b"XMP "}]

    result = _run_worker(app_module, [_item(source, "WebP Managed Title")], output_dir=output_dir)
    output = output_dir / "WebP_Managed_Title.webp"

    assert result["ok"] == 1
    after = _image_snapshot(output)
    assert after["pixel"] == before["pixel"]
    assert after["alpha"] == before["alpha"]
    assert after["icc"] == before["icc"]
    retained = [raw for kind, _payload, raw in _webp_chunks(output.read_bytes()) if kind not in {b"VP8X", b"EXIF", b"XMP "}]
    assert retained == protected
    assert _webp_payloads(output, b"ICCP") == _webp_payloads(source, b"ICCP")
    assert _webp_payloads(output, b"UNKN") == [b"Keep unknown RIFF payload"]
    flags = _webp_payloads(output, b"VP8X")[0][0]
    assert flags & 0x20 and flags & 0x10 and flags & 0x08 and flags & 0x04
    output_xmp = ET.fromstring(_webp_payloads(output, b"XMP ")[0])
    assert _xmp_alt(output_xmp, "title") == "WebP Managed Title"
    assert _xmp_value(output_xmp, ".//dc:creator/rdf:Seq/rdf:li") == "Current Creator"
    _assert_common_metadata(source, output, container="webp")


def test_static_webp_blank_creator_keeps_identity_rights_dates_and_location(app_module, tmp_path):
    source = tmp_path / "source.webp"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_webp(source)

    result = _run_worker(
        app_module,
        [_item(source, "WebP Blank Creator", description="", keywords=[])],
        output_dir=output_dir,
        creator="",
    )
    output = output_dir / "WebP_Blank_Creator.webp"
    output_exif = _exif_from_payload(_webp_payloads(output, b"EXIF")[0])

    assert result["ok"] == 1
    assert output_exif["0th"][piexif.ImageIFD.Artist] == b"Legacy Creator"
    assert _xmp_value(ET.fromstring(_webp_payloads(output, b"XMP ")[0]), ".//dc:creator/rdf:Seq/rdf:li") == "Legacy Creator"
    _assert_common_metadata(source, output, container="webp")


def test_animated_webp_is_blocked_without_output_or_card_success(app_module, tmp_path):
    source = tmp_path / "animated.webp"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_animated_webp(source)
    source_hash = _sha256(source)
    card = object()

    result = _run_worker(app_module, [_item(source, "Animated WebP", card=card)], output_dir=output_dir)

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert "does not support safe saving of animated WebP" in result["errors"][0]
    assert _sha256(source) == source_hash
    assert not list(output_dir.iterdir())


@pytest.mark.parametrize("mutation", ["riff-size", "padding"])
def test_malformed_webp_blocks_save_without_final_output(app_module, tmp_path, mutation):
    source = tmp_path / f"bad-{mutation}.webp"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_webp(source)
    raw = bytearray(source.read_bytes())
    if mutation == "riff-size":
        raw[4:8] = (len(raw) + 200).to_bytes(4, "little")
    else:
        chunks = _webp_chunks(bytes(raw))
        odd = next(raw_chunk for _kind, payload, raw_chunk in chunks if len(payload) % 2)
        offset = bytes(raw).index(odd) + len(odd) - 1
        raw[offset] = 0x7F
    source.write_bytes(raw)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source, "Blocked WebP")], output_dir=output_dir)

    assert result["ok"] == 0
    assert result["saved_paths"] == []
    assert _sha256(source) == source_hash
    assert not list(output_dir.iterdir())


@pytest.mark.parametrize("suffix,maker", [(".png", _make_png), (".webp", _make_webp)])
def test_png_webp_same_name_and_existing_targets_never_overwrite(app_module, tmp_path, suffix, maker):
    source = tmp_path / f"Same_Name_Title{suffix}"
    maker(source)
    source_hash = _sha256(source)
    existing = tmp_path / f"Same_Name_Title_1{suffix}"
    existing.write_bytes(b"existing output must remain unchanged")
    existing_hash = _sha256(existing)

    result = _run_worker(app_module, [_item(source, "Same Name Title")], output_dir=None)
    output = tmp_path / f"Same_Name_Title_2{suffix}"

    assert result["ok"] == 1
    assert result["saved_paths"] == [str(output)]
    assert _sha256(source) == source_hash
    assert _sha256(existing) == existing_hash
    assert output.exists()


@pytest.mark.parametrize("suffix,maker", [(".png", _make_png), (".webp", _make_webp)])
def test_png_webp_same_batch_title_reservations_are_distinct(app_module, tmp_path, suffix, maker):
    first = tmp_path / f"first{suffix}"
    second = tmp_path / f"second{suffix}"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    maker(first)
    maker(second)
    first_hash = _sha256(first)
    second_hash = _sha256(second)

    result = _run_worker(
        app_module,
        [_item(first, "Twin/Name"), _item(second, "Twin:Name")],
        output_dir=output_dir,
    )

    assert result["ok"] == 2
    assert result["saved_paths"] == [str(output_dir / f"Twin_Name{suffix}"), str(output_dir / f"Twin_Name_1{suffix}")]
    assert _sha256(first) == first_hash
    assert _sha256(second) == second_hash


@pytest.mark.parametrize("suffix,maker", [(".png", _make_png), (".webp", _make_webp)])
def test_png_webp_save_to_input_folder_never_replaces_source(app_module, tmp_path, suffix, maker):
    source = tmp_path / f"Folder_Same_Title{suffix}"
    maker(source)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source, "Folder Same Title")], output_dir=tmp_path)

    output = tmp_path / f"Folder_Same_Title_1{suffix}"
    assert result["ok"] == 1
    assert result["saved_paths"] == [str(output)]
    assert _sha256(source) == source_hash
    assert output.exists()


@pytest.mark.parametrize(
    ("suffix", "maker", "merge_name"),
    [
        (".png", _make_png, "_merge_png_metadata"),
        (".webp", _make_webp, "_merge_webp_metadata"),
    ],
)
def test_png_webp_metadata_merge_failure_never_publishes_or_marks_saved(app_module, tmp_path, monkeypatch, suffix, maker, merge_name):
    source = tmp_path / f"source{suffix}"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    maker(source)
    source_hash = _sha256(source)
    monkeypatch.setattr(
        app_module.metadata,
        merge_name,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic container metadata failure")),
    )

    result = _run_worker(app_module, [_item(source, "Failed Container Metadata")], output_dir=output_dir)

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert "synthetic container metadata failure" in result["errors"][0]
    assert _sha256(source) == source_hash
    assert not list(output_dir.iterdir())


@pytest.mark.parametrize("suffix,maker", [(".png", _make_png), (".webp", _make_webp)])
def test_png_webp_raw_staging_failure_never_publishes_or_marks_saved(app_module, tmp_path, monkeypatch, suffix, maker):
    source = tmp_path / f"source{suffix}"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    maker(source)
    source_hash = _sha256(source)
    monkeypatch.setattr(
        app_module.metadata,
        "_write_container_bytes_to_tempfile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic container staging failure")),
    )

    result = _run_worker(app_module, [_item(source, "Failed Container Staging")], output_dir=output_dir)

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert "synthetic container staging failure" in result["errors"][0]
    assert _sha256(source) == source_hash
    assert not list(output_dir.iterdir())


@pytest.mark.parametrize(
    ("suffix", "maker", "verify_name"),
    [
        (".png", _make_png, "_verify_png_written_metadata"),
        (".webp", _make_webp, "_verify_webp_written_metadata"),
    ],
)
def test_png_webp_readback_failure_never_publishes_or_marks_saved(app_module, tmp_path, monkeypatch, suffix, maker, verify_name):
    source = tmp_path / f"source{suffix}"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    maker(source)
    source_hash = _sha256(source)
    monkeypatch.setattr(
        app_module.metadata,
        verify_name,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("synthetic container readback failure")),
    )

    result = _run_worker(app_module, [_item(source, "Failed Container Readback")], output_dir=output_dir)

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert "synthetic container readback failure" in result["errors"][0]
    assert _sha256(source) == source_hash
    assert not list(output_dir.iterdir())


@pytest.mark.parametrize("suffix,maker", [(".png", _make_png), (".webp", _make_webp)])
def test_png_webp_publish_race_uses_next_safe_name_without_overwrite(app_module, tmp_path, monkeypatch, suffix, maker):
    source = tmp_path / f"source{suffix}"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    maker(source)
    external_target = output_dir / f"Container_Race{suffix}"
    external_bytes = b"external output must remain unchanged"
    original_publish = app_module._publish_new_output
    calls = []

    def race_publish(temp_path, save_path):
        calls.append(Path(save_path))
        if len(calls) == 1:
            Path(save_path).write_bytes(external_bytes)
            raise FileExistsError("synthetic external publish race")
        return original_publish(temp_path, save_path)

    monkeypatch.setattr(app_module.metadata, "_publish_new_output", race_publish)
    result = _run_worker(app_module, [_item(source, "Container Race")], output_dir=output_dir)

    assert result["ok"] == 1
    assert external_target.read_bytes() == external_bytes
    assert result["saved_paths"] == [str(output_dir / f"Container_Race_1{suffix}")]
    assert (output_dir / f"Container_Race_1{suffix}").exists()
    assert not list(output_dir.glob(".pixtagra_*.tmp"))
