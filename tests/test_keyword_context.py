"""Quality regressions observed in actual image-model outputs."""
import subprocess
import sys

import pixtagra_keywords as policy


def test_chinese_opera_record_flags_unsubstantiated_japanese_mask_keyword():
    warnings = policy.metadata_context_warnings(
        "Traditional Chinese opera performer with colorful face masks",
        "An illustration of a Chinese opera performer with embroidered robes and red lanterns.",
        ["chinese opera", "opera mask", "japanese mask", "red lantern"],
    )
    assert len(warnings) == 1 and "japanese mask" in warnings[0]


def test_visible_mixed_cultural_context_and_unspecified_origin_are_not_rejected():
    assert policy.metadata_context_warnings(
        "Chinese and Japanese masks on display", "A collection of different theatrical masks.",
        ["chinese opera", "japanese mask"],
    ) == []
    assert policy.metadata_context_warnings("Theatrical mask collection", "A display of colorful masks.", ["japanese mask"]) == []


def test_cultural_warning_is_reviewable_and_keeps_user_keywords():
    keywords = ["chinese opera", "japanese mask"]
    _, review = policy.classify_stock_metadata(
        "Traditional Chinese opera performer with colorful face masks",
        "An illustration of a Chinese opera performer in an ornate costume. Painted masks surround the central figure, with red lanterns and embroidered golden dragons visible across the stage and flowing robes.",
        keywords,
    )
    assert any(issue.startswith("Potential keyword context mismatch:") for issue in review)
    assert keywords == ["chinese opera", "japanese mask"]


def test_policy_and_model_catalog_import_without_gui_network_or_credential_modules():
    # Real clean interpreter, not a GUI stub: business rules stay independent.
    result = subprocess.run([sys.executable, "-c", (
        "import sys; import pixtagra_keywords; import pixtagra_models; "
        "assert not any(n in sys.modules for n in ('tkinter','customtkinter','requests','pixtagra_credentials','pixtagra'))"
    )], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
