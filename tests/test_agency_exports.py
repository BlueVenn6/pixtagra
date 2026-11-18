"""Validate real agency documents and actual exported bytes; no live submission."""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path

from PIL import Image
import pytest

import pixtagra_exports as exports


def data():
    return {
        "title": "Mountain and Lake Landscape beneath Clouds in Evening Light",
        "description": "Mountains rise above a lake with trees along the water and clouds overhead. Evening light falls across the landscape, showing the rocky slopes and the still water in a wide view.",
        "keywords": ["mountain", "lake", "forest", "water", "clouds", "sky", "landscape"],
        "metadata_language": "en", "compliance": {},
    }


def decode(target, record=None, path="photo.jpg"):
    payload = exports.csv_bytes(target, [exports.csv_row(target, path, record or data())])
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))[0]


def test_adobe_header_matches_unmodified_official_download():
    sample = Path(__file__).parent / "fixtures/agency_exports/Sample_Adobe_Stock_CSV_upload.csv"
    assert hashlib.sha256(sample.read_bytes()).hexdigest() == "41386ff0451921bdaa80b30ac91b9ed924e66f6d0598a97069a9d76a6e12aa62"
    with sample.open(encoding="utf-8-sig", newline="") as stream:
        assert tuple(next(csv.reader(stream))) == exports.get_target("adobe").headers
    row = decode("adobe")
    assert list(row) == ["Filename", "Title", "Keywords", "Category", "Releases"]
    assert row["Category"] == row["Releases"] == ""  # Optional, never invent clearances.
    assert row["Title"] == data()["title"]


@pytest.mark.parametrize("target", ["adobe", "shutterstock", "getty"])
def test_csv_round_trip_preserves_quotes_unicode_filenames_and_keyword_order(target):
    record = data()
    original = copy.deepcopy(record)
    row = decode(target, record, '照片, "1".jpg')
    filename_column = "file name" if target == "getty" else "Filename"
    keyword_column = "keywords" if target == "getty" else "Keywords"
    assert row[filename_column] == '照片, "1".jpg'
    assert row[keyword_column].split(", ") == record["keywords"]
    assert record == original


def test_shutterstock_exact_columns_enums_and_categories_from_official_csv_guide():
    record = data()
    record["compliance"] = dict(editorial_context=True, mature_content=True, illustration_or_render=True)
    row = exports.csv_row("shutterstock", "image.jpg", record, ("Animals/Wildlife", "Nature"))
    assert list(row) == ["Filename", "Description", "Keywords", "Categories", "Illustration", "Mature Content", "Editorial"]
    assert [row[key] for key in ("Illustration", "Mature Content", "Editorial")] == ["Yes"] * 3
    assert row["Categories"] == "Animals/Wildlife, Nature"
    with pytest.raises(ValueError):
        exports.csv_row("shutterstock", "image.jpg", record, ("Animals and Wildlife", "Nature"))


def test_shutterstock_current_2048_character_limit_and_seven_keyword_minimum():
    record = data()
    record["description"] = "a" * 2048
    assert not exports.record_errors("shutterstock", "image.jpg", record)
    record["description"] += "a"
    assert "2048" in " ".join(exports.record_errors("shutterstock", "image.jpg", record))
    record = data()
    record["keywords"].pop()
    assert "7–50" in " ".join(exports.record_errors("shutterstock", "image.jpg", record))


def test_getty_is_deepmeta_import_with_english_captions_and_no_invented_term_ids():
    row = decode("getty")
    assert list(row) == ["file name", "title", "description", "keywords"]
    assert row["description"] == data()["description"]
    assert "DeepMeta" in exports.get_target("getty").label


@pytest.mark.parametrize("target", ["getty", "alamy", "shutterstock"])
def test_chinese_is_blocked_without_silent_translation(target):
    record = data()
    record.update(title="木质桌面上的白色咖啡杯", metadata_language="zh")
    original = copy.deepcopy(record)
    assert "English" in " ".join(exports.record_errors(target, "image.jpg", record))
    assert not exports.record_errors("adobe", "image.jpg", record)
    assert record == original


@pytest.mark.parametrize("field,size", [("title", 71), ("filename", 31)])
def test_adobe_overlong_fields_are_blocked_not_truncated(field, size):
    record, path = data(), "image.jpg"
    if field == "title":
        record["title"] = "a" * size
    else:
        path = "a" * (size - 4) + ".jpg"
    assert exports.record_errors("adobe", path, record)
    assert len(record["title"] if field == "title" else path) == size


@pytest.mark.parametrize("target", ["adobe", "getty", "alamy", "shutterstock", "vcg"])
def test_generated_twenty_keyword_gate_cannot_be_overridden_by_export_target(target):
    record = data()
    record["keyword_policy_version"] = 3
    assert "20–35" in " ".join(exports.record_errors(target, "image.jpg", record))


def test_twenty_duplicate_terms_do_not_satisfy_the_generated_minimum():
    record = data()
    record.update(keyword_policy_version=3, keywords=["mountain"] * 20)
    assert "found 1" in " ".join(exports.record_errors("adobe", "image.jpg", record))


def test_adobe_file_limits_reject_entire_batch_before_writing():
    row = exports.csv_row("adobe", "image.jpg", data())
    with pytest.raises(ValueError, match="5000"):
        exports.csv_bytes("adobe", [row] * 5001)
    row["Keywords"] = "a" * 1_000_000
    with pytest.raises(ValueError, match="1 MB"):
        exports.csv_bytes("adobe", [row])


def test_adobe_current_csv_fifty_keyword_limit_without_changing_generated_policy():
    record = data()
    record["keywords"] = [f"term {index}" for index in range(50)]
    assert not exports.record_errors("adobe", "image.jpg", record)
    record["keywords"].append("one more term")
    assert "50" in " ".join(exports.record_errors("adobe", "image.jpg", record))


@pytest.mark.parametrize("separator", [",", ";", "|"])
def test_keyword_with_embedded_separator_is_rejected_instead_of_changing_term_count(separator):
    record = data()
    record["keywords"][0] = "a" + separator + "b"
    assert "separator" in " ".join(exports.record_errors("getty", "image.jpg", record))


def test_alamy_export_writes_verified_iptc_copies_and_preserves_source_pixels(tmp_path):
    import pixtagra_metadata as metadata
    source = tmp_path / "原始照片.jpg"
    Image.new("RGB", (96, 64), "red").save(source)
    original = source.read_bytes()
    progress = []
    record = data()
    destination = exports.alamy_jpeg_bundle([{"path": str(source), "data": record}], tmp_path,
        progress=lambda done, total: progress.append((done, total)))
    output = destination / source.name
    metadata._verify_written_metadata(output, record["title"], record["title"], record["keywords"], source_jpeg_path=source)
    with Image.open(source) as before, Image.open(output) as after:
        assert before.tobytes() == after.tobytes()
    assert source.read_bytes() == original
    manifest = json.loads((destination / "metadata.json").read_text(encoding="utf8"))
    assert manifest[0]["description"] == record["description"]
    assert manifest[0]["caption"] == record["title"]
    assert manifest[0]["keywords"] == record["keywords"]
    assert progress == [(1, 1)]


def test_failed_alamy_batch_does_not_publish_partial_output_or_touch_existing_files(tmp_path):
    source = tmp_path / "image.jpg"
    Image.new("RGB", (96, 64), "red").save(source)
    original = source.read_bytes()
    existing = tmp_path / "existing.txt"
    existing.write_text("keep", encoding="utf8")
    Image.new("RGB", (96, 64), "blue").save(tmp_path / "image.png")
    with pytest.raises(ValueError, match="JPEG"):
        exports.alamy_jpeg_bundle([{"path": str(source), "data": data()},
            {"path": str(tmp_path / "image.png"), "data": data()}], tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing.txt", "image.jpg", "image.png"]
    assert source.read_bytes() == original and existing.read_text() == "keep"


def test_alamy_source_replaced_after_snapshot_is_blocked(tmp_path):
    source = tmp_path / "image.jpg"
    Image.new("RGB", (96, 64), "red").save(source)
    with pytest.raises(ValueError, match="changed"):
        exports.alamy_jpeg_bundle([{"path": str(source), "data": data(), "source_identity": [0, 0]}], tmp_path)
    assert list(tmp_path.iterdir()) == [source]


def test_vcg_reference_is_explicitly_manual_and_does_not_claim_an_import_schema():
    target = exports.get_target("vcg")
    assert "not an upload template" in target.hint
    assert "Manual entry" in target.label
    row = decode("vcg")
    assert row["关键词（手动录入参考）"].split(", ") == data()["keywords"]
    assert row["核心关键词建议（后台确认）"].split(", ") == data()["keywords"][:5]
    assert row["标题"] == data()["title"]
    record = data()
    record["keywords"] *= 6
    assert "5–35" in " ".join(exports.record_errors("vcg", "image.jpg", record))
