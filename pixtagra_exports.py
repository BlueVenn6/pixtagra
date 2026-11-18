"""Agency-specific export formats, independent from generation and credentials.

Official references and verification limits are in docs/reports/agency-export-20261108.md.
This module does not translate, truncate, supplement, or reorder metadata.
"""
import csv
from dataclasses import dataclass
import io
import json
from pathlib import Path
import shutil
import tempfile
import uuid

from pixtagra_language import metadata_language
from pixtagra_keywords import _contains_non_english_text, normalize_compliance_flags


@dataclass(frozen=True)
class ExportTarget:
    key: str
    label: str
    kind: str
    hint: str
    source: str
    headers: tuple = ()


TARGETS = (
    ExportTarget("shutterstock", "Shutterstock · CSV", "csv",
        "English metadata; upload the matching original filenames before the CSV.",
        "https://submit.shutterstock.com/help/en/articles/10617486-how-do-i-include-existing-metadata-with-my-content-submission",
        ("Filename", "Description", "Keywords", "Categories", "Illustration", "Mature Content", "Editorial")),
    ExportTarget("adobe", "Adobe Stock · CSV", "csv",
        "Titles up to 70 characters; review category and releases in Adobe Stock.",
        "https://helpx.adobe.com/stock/contributor/manage-your-portfolio/csv-requirements-content.html",
        ("Filename", "Title", "Keywords", "Category", "Releases")),
    ExportTarget("alamy", "Alamy · IPTC JPEG", "jpeg",
        "English titles become captions in JPEG copies; originals and workspace are preserved.",
        "https://www.alamy.com/help/contributor-image-management/"),
    ExportTarget("getty", "Getty/iStock · DeepMeta CSV", "csv",
        "English titles/descriptions; import with DeepMeta 4 and confirm keyword matches.",
        "https://www.deepmeta.com/blog/post/deepmeta-uploads",
        ("file name", "title", "description", "keywords")),
    ExportTarget("vcg", "VCG · Manual entry reference", "csv",
        "Manual entry reference, not an upload template. Confirm 1–5 core keywords in VCG.",
        "https://creatorstudio.500px.com.cn/faq",
        ("文件名", "标题", "关键词（手动录入参考）", "核心关键词建议（后台确认）", "描述（参考）")),
)
BY_KEY = {t.key: t for t in TARGETS}
BY_LABEL = {t.label: t for t in TARGETS}

SS_CATEGORIES = frozenset((
    "Abstract", "Animals/Wildlife", "Arts", "Backgrounds/Textures", "Beauty/Fashion",
    "Buildings/Landmarks", "Business/Finance", "Celebrities", "Education", "Food and Drink",
    "Healthcare/Medical", "Holidays", "Industrial", "Interiors", "Miscellaneous", "Nature",
    "Objects", "Parks/Outdoor", "People", "Religion", "Science", "Signs/Symbols",
    "Sports/Recreation", "Technology", "Transportation", "Vintage",
))


def get_target(value="shutterstock"):
    if isinstance(value, ExportTarget):
        return value
    if value in BY_KEY:
        return BY_KEY[value]
    if value in BY_LABEL:
        return BY_LABEL[value]
    raise ValueError("Unknown export target")


def record_errors(target, path, data):
    """Blocking format rules; content quality warnings remain separate."""
    target = get_target(target)
    if target.kind == "unverified":
        return [target.hint]
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    keywords = data.get("keywords", [])
    errors = []
    if not title or not description or not isinstance(keywords, (list, tuple)) or not keywords:
        return ["Title, description and keywords are required."]
    if any(not isinstance(k, str) or not k.strip() for k in keywords):
        return ["Keywords must be non-empty text values."]
    if any("," in k or ";" in k or "|" in k for k in keywords):
        errors.append("One keyword contains a separator; split it into separate keywords before export.")
    if any(c in title + description + "".join(keywords) for c in ("\x00",)):
        errors.append("Metadata contains a null character.")
    unique_count = len({keyword.strip().casefold() for keyword in keywords})
    if data.get("keyword_policy_version") == 3 and (unique_count < 20 or len(keywords) > 35):
        errors.append(f"Generated metadata needs 20–35 final keywords; found {unique_count}.")
    if target.key in {"shutterstock", "alamy", "getty"}:
        text = title + description + " ".join(keywords)
        if metadata_language(data) != "en" or _contains_non_english_text(text):
            errors.append("This export requires English metadata. Existing text is not translated automatically.")
    if target.key == "shutterstock":
        if not 7 <= len(keywords) <= 50:
            errors.append("Shutterstock requires 7–50 keywords.")
        if len(description) > 2048:
            errors.append("Shutterstock descriptions allow at most 2048 characters.")
    elif target.key == "adobe":
        if len(Path(path).name) > 30:
            errors.append("Adobe Stock filenames allow at most 30 characters including the extension.")
        if len(title) > 70:
            errors.append("Adobe Stock titles allow at most 70 characters.")
        if any(c in title for c in (",", "\r", "\n")):
            errors.append("Adobe Stock CSV titles cannot contain commas or line breaks.")
        if len(keywords) > 50:
            errors.append("Adobe Stock export allows at most 50 keywords.")
    elif target.key == "getty" and len(keywords) > 50:
        errors.append("Getty/iStock allows at most 50 keyword terms.")
    elif target.key == "vcg" and not 5 <= len(keywords) <= 35:
        errors.append("VCG allows 5–35 keywords; choose 1–5 core keywords in the contributor portal.")
    elif target.key == "alamy" and Path(path).suffix.lower() not in {".jpg", ".jpeg"}:
        errors.append("Alamy IPTC export accepts JPEG originals only; no image conversion is performed.")
    return errors


def csv_row(target, path, data, categories=("Objects", "")):
    target = get_target(target)
    filename = Path(path).name
    keywords = ", ".join(data["keywords"])
    if target.key == "adobe":
        # Both fields are optional. Do not invent a numeric category or release
        # filename: Adobe supplies category suggestions and binds uploaded releases.
        return dict(zip(target.headers, (filename, data["title"], keywords, "", "")))
    if target.key == "getty":
        return dict(zip(target.headers, (filename, data["title"], data["description"], keywords)))
    if target.key == "vcg":
        # App-defined reference columns, explicitly NOT a claimed VCG import
        # schema. The first five terms are suggestions; only the contributor
        # can select actual core terms in VCG's published manual-entry workflow.
        return dict(zip(target.headers, (filename, data["title"], keywords,
            ", ".join(data["keywords"][:5]), data["description"])))
    if target.key != "shutterstock":
        raise ValueError("This target does not use CSV")
    chosen = [c for c in categories if c]
    if not 1 <= len(chosen) <= 2 or any(c not in SS_CATEGORIES for c in chosen):
        raise ValueError("Shutterstock needs one or two official category names")
    flags = normalize_compliance_flags(data.get("compliance", {}))
    return dict(zip(target.headers, (filename, data["description"], keywords, ", ".join(chosen),
        "Yes" if flags["illustration_or_render"] else "No",
        "Yes" if flags["mature_content"] else "No",
        "Yes" if flags["editorial_context"] else "No")))


def csv_bytes(target, rows):
    target = get_target(target)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=target.headers)
    writer.writeheader()
    writer.writerows(rows)
    result = stream.getvalue().encode("utf-8-sig")
    if target.key == "adobe" and (len(rows) > 5000 or len(result) > 1_000_000):
        raise ValueError("Adobe Stock CSV must be at most 5000 rows and 1 MB; export a smaller batch.")
    return result


def alamy_jpeg_bundle(records, parent, progress=None):
    """Publish verified copies in a new folder; never write to an input image.

    The short English title maps to IPTC Caption. The full original text is kept
    in metadata.json. The already-tested JPEG merge and independent verifier are
    reused; no duplicate image encoder or metadata writer is introduced.
    """
    import pixtagra_metadata as metadata
    from pixtagra_session import image_identity
    parent = Path(parent).resolve()
    staging = Path(tempfile.mkdtemp(prefix=".pixtagra_alamy_", dir=parent))
    destination = parent / ("PixTagra_Alamy_" + uuid.uuid4().hex[:12])
    try:
        names = set()
        manifest = []
        for index, record in enumerate(records, 1):
            path, data = Path(record["path"]), record["data"]
            identity = record.get("source_identity", image_identity(path))
            if identity is None or image_identity(path) != identity:
                raise ValueError("Source image changed or is missing: " + path.name)
            errors = record_errors("alamy", path, data)
            if errors:
                raise ValueError(path.name + ": " + errors[0])
            if path.name.casefold() in names:
                raise ValueError("Duplicate image filename: " + path.name)
            names.add(path.name.casefold())
            caption = data["title"]
            content = metadata.inject_metadata(path, data["title"], caption, data["keywords"], preserve_existing=True)
            output = staging / path.name
            output.write_bytes(content)
            metadata._verify_written_metadata(output, data["title"], caption, data["keywords"], source_jpeg_path=path)
            if image_identity(path) != identity:
                raise ValueError("Source image changed during export: " + path.name)
            manifest.append({"file": path.name, "title": data["title"], "description": data["description"],
                             "caption": caption, "keywords": data["keywords"]})
            if progress:
                progress(index, len(records))
        (staging / "metadata.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf8")
        (staging / "README.txt").write_text(
            "Alamy: upload these JPEG copies, not this folder's JSON.\n"
            "IPTC Caption uses your English title; keywords retain their order.\n"
            "The full description is preserved in metadata.json. Originals are unchanged.\n"
            "Review Alamy's image specifications, dates, releases, licence type and QC requirements before submission.\n"
            "Official help: https://www.alamy.com/help/contributor-image-management/\n", encoding="utf8")
        staging.rename(destination)
        return destination
    except Exception:
        # This is the exact fresh directory created above, never a user directory.
        if staging.parent.resolve() != parent or not staging.name.startswith(".pixtagra_alamy_"):
            raise RuntimeError("Refusing cleanup outside owned export staging directory")
        shutil.rmtree(staging)
        raise
