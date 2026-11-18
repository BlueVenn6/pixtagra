from pixtagra_keywords import sanitize_keywords_for_stock, metadata_context_warnings, metadata_compliance_warnings
from pixtagra_quality import apply_keyword_review, keyword_review_warnings


def test_chinese_search_variants_merge_without_collapsing_different_subjects():
    kept, rejected, _ = sanitize_keywords_for_stock(
        ["咖啡杯", "咖啡杯子", "盛咖啡的杯子", "咖啡", "茶杯", "玻璃杯", "杯子蛋糕", "木制桌面", "木质桌面", "金属桌面"], language="zh")
    assert kept == ["咖啡杯", "咖啡", "茶杯", "玻璃杯", "杯子蛋糕", "木制桌面", "金属桌面"]
    assert len(rejected) == 3


def test_live_sample_dairy_cow_synonyms_do_not_count_twice():
    kept, rejected, _ = sanitize_keywords_for_stock(
        ["奶牛", "乳牛", "黄牛", "水牛", "牛奶", "奶牛场", "乳牛场"], language="zh")
    assert kept == ["奶牛", "黄牛", "水牛", "牛奶", "奶牛场"]
    assert [item[0] for item in rejected] == ["乳牛", "乳牛场"]


def test_chinese_culture_conflicts_are_warnings_and_mixed_scenes_are_allowed():
    assert metadata_context_warnings("中国京剧脸谱的彩色绘画", "京剧脸谱绘画。", ["日本歌舞伎"])
    assert metadata_context_warnings("Japanese torii gate", "A gate in Japan.", ["中国京剧"])
    assert not metadata_context_warnings("中日文化展览中的京剧与歌舞伎", "中国和日本文化并列呈现。", ["京剧", "歌舞伎"])
    assert not metadata_context_warnings("咖啡杯", "桌面上有咖啡。", ["陶瓷", "咖啡"])


def test_chinese_rights_fallbacks_flag_risks_without_asserting_permissions():
    words = ["人物", "商标", "壁画", "私人室内", "新闻事件", "AI生成", "数字插画", "裸体"]
    warnings = metadata_compliance_warnings("", "", words, {})
    assert len(warnings) == 8
    assert any("model releases" in warning for warning in warnings)
    assert any("AI disclosure" in warning for warning in warnings)
    assert not metadata_compliance_warnings("木纹背景与留白", "纸张表面纹理。", ["纸张", "木纹", "留白"], {})


def test_chinese_visual_review_does_not_claim_provenance_from_appearance():
    candidates = ["京剧脸谱", "中国戏曲", "官方插画", "AI生成", "歌舞伎", "面具"]
    result = apply_keyword_review({"keywords": candidates}, {"checks": [
        {"id": i, "verdict": "supported", "reason": "fixture review"} for i in range(len(candidates))]}, "Qwen 3.7 Plus")
    assert "官方插画" not in result["keywords"] and "AI生成" not in result["keywords"]
    assert any("京剧" in issue for issue in keyword_review_warnings(result))
    assert "中国戏曲" in result["keywords"] and "面具" in result["keywords"]
