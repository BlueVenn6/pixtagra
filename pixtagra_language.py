"""Explicit user language and per-record metadata language; no OS inference."""

import re

LANGUAGE_OPTIONS = {"English": "en", "简体中文": "zh"}
LANGUAGE_NAMES = {code: label for label, code in LANGUAGE_OPTIONS.items()}


def normalize_language(value):
    return "zh" if value in ("zh", "zh-CN", "简体中文") else "en"


def metadata_language(data):
    if data.get("metadata_language") in ("en", "zh"):
        return data["metadata_language"]
    # Older/manual records have no language field. Use their actual captions,
    # never the current dropdown, so switching UI language cannot corrupt them.
    text = str(data.get("title", "")) + str(data.get("description", ""))
    if not text.strip():
        text = " ".join(map(str, data.get("keywords", [])))
    return "zh" if re.search(r"[\u3400-\u9fff]", text) else "en"


def metadata_options(data):
    language = metadata_language(data)
    options = {"language": language} if language == "zh" or "metadata_language" in data else {}
    if data.get("keyword_language") and data["keyword_language"] != language:
        options["keyword_language"] = data["keyword_language"]
    if data.get("keyword_policy_version") in (2, 3):
        options["generated"] = True
        if data["keyword_policy_version"] == 3:
            options["keyword_policy_version"] = 3
    return options


def split_keywords(value):
    return [item.strip() for item in re.split(r"[;,；，、\n]+", str(value or "")) if item.strip()]
