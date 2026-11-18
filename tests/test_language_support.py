"""Language policy does not depend on Windows locale or translate old data."""
import pytest

from pixtagra_keywords import (PROMPT_MODES, build_prompt, build_metadata_repair_prompt,
    sanitize_keywords_for_stock, validate_stock_metadata, rank_keywords_for_stock)
from pixtagra_language import metadata_language, metadata_options, normalize_language, split_keywords
from pixtagra_session import metadata_only


@pytest.mark.parametrize('source, expected', [
    ('All records passed. Ready to save.', '全部记录已通过检查，可以保存。'),
    ('2 ready to save | 1 need review | 3 failed', '2 可保存 | 1 待检查 | 3 失败'),
    ('Confirm retry', '确认重试'),
    ('Retry 2', '重试 2 张'),
    ('Retry 2 image(s)?', '重试 2 张图片？'),
    ('Retry generation (2)', '重新生成 (2)'),
    ('Save with review (2)', '检查后保存 (2)'),
    ('2 record(s) are ready to save.', '2 条记录可以保存。'),
])
def test_completed_batch_summary_is_translated_without_changing_counts(source, expected):
    from pixtagra_localization import translate
    assert translate(source, 'zh') == expected
    assert translate(source, 'en') == source


def chinese_metadata():
    return {
        "title": "晨光下的陶瓷咖啡杯与木质桌面",
        "description": "一只白色陶瓷咖啡杯放在木质桌面上，杯中盛着深色咖啡，旁边散落着几颗咖啡豆。柔和晨光从侧面照亮杯身，呈现清晰的杯柄和自然木纹。",
        "keywords": ["咖啡杯", "咖啡", "陶瓷", "木质桌面", "咖啡豆", "杯柄", "白色", "木纹", "侧光", "静物"],
        "metadata_language": "zh", "keyword_language": "zh", "compliance": {},
    }


@pytest.mark.parametrize("mode", PROMPT_MODES)
def test_selected_language_reaches_generation_and_correction_prompts(mode):
    english = build_prompt(mode)
    chinese = build_prompt(mode, "简体中文")
    assert "English only" in english and "Simplified Chinese only" in chinese
    assert "English keywords" not in chinese and "English phrase" not in chinese
    assert "8 to 30 Chinese characters" in chinese and "30 to 120 Chinese characters" in chinese
    assert "Keep JSON keys unchanged" in chinese
    # Both modes use the selected language and the new acceptance interval.
    assert "20 to 35" in english and "20 to 35" in chinese
    assert "Aim for 35" not in english and "tag35" not in chinese
    assert "Simplified Chinese only" in build_metadata_repair_prompt(chinese_metadata(), ["Title"], mode)


def test_chinese_sanitizer_preserves_meaningful_phrases_and_rejects_duplicates():
    kept, rejected, _ = sanitize_keywords_for_stock(
        ["咖啡杯", "咖啡", "咖啡杯", "木质桌面", "超高清", "coffee", "陶瓷"], language="zh")
    assert kept == ["咖啡杯", "咖啡", "木质桌面", "陶瓷"]
    assert len(rejected) == 3
    assert split_keywords("咖啡杯；咖啡，陶瓷、木质桌面\n木纹") == ["咖啡杯", "咖啡", "陶瓷", "木质桌面", "木纹"]


def test_chinese_validation_counts_characters_and_recognizes_chinese_sentences():
    data = chinese_metadata()
    assert not validate_stock_metadata(data["title"], data["description"], data["keywords"], **metadata_options(data))
    issues = validate_stock_metadata("杯", "一只白色咖啡杯。", data["keywords"], language="zh")
    assert any("Title has 1 Chinese characters" in issue for issue in issues)
    assert any("Description has 1 sentences" in issue for issue in issues)


def test_legacy_language_is_inferred_from_record_and_survives_session_filtering():
    data = chinese_metadata()
    assert metadata_language({"title": data["title"]}) == "zh"
    assert metadata_language({"title": "A coffee cup"}) == "en"
    assert normalize_language(None) == "en"
    assert metadata_only({**data, "api_key": "SYNTHETIC_DO_NOT_USE"}) == data


def test_keyword_only_regeneration_can_use_chinese_with_preserved_english_captions():
    data = chinese_metadata()
    data.update(title="White Ceramic Coffee Cup on a Wooden Table in Sunlight",
        description="A white ceramic coffee cup stands on a wooden table beside scattered coffee beans. Soft light enters from the side, showing the curved handle and the natural texture of the wood beneath it.",
        metadata_language="en")
    assert not validate_stock_metadata(data["title"], data["description"], data["keywords"], **metadata_options(data))


def test_chinese_background_mode_uses_visible_background_evidence():
    keywords = rank_keywords_for_stock(["纸张背景", "纸张", "纹理", "白色", "留白", "海报"], PROMPT_MODES[1])
    issues = validate_stock_metadata("白色纸张表面的细腻纹理与留白背景",
        "白色纸张表面呈现细腻的纤维纹理，柔和光线照亮画面，边缘带有轻微阴影。大片留白保留了清晰的纸面质感，可作为海报排版的背景。",
        keywords, PROMPT_MODES[1], language="zh")
    assert not issues
