"""Stock metadata policy, prompts, normalization and quality checks.

Pure Python: importing or testing these rules does not create a GUI, read
credentials, access files or call a model. Both editors and model output use
this same policy.
"""

import json
import re
import unicodedata
from pixtagra_language import metadata_language, normalize_language

PROMPT_MODES = ["📷 General Photography", "🎨 Commercial Background"]


# ONLY block genuinely generic/filler terms that add zero search value
# Do NOT block content-descriptive words (romantic, valentine etc.)
# as they may be accurate for certain images
KEYWORD_BLACKLIST = {
    # Generic filler
    "stock photo", "stock image", "stock photography", "royalty free image",
    "professional quality", "high quality photo", "quality image",
    "creative design element", "design element", "graphic element",
    "beautiful", "stunning", "amazing", "gorgeous", "wonderful", "breathtaking",
    "eye-catching", "nice", "lovely", "perfect", "incredible",
    "photo", "image", "picture", "photograph", "imagery",
    "background image", "wallpaper image",
    "digital art", "ai generated", "ai art", "computer generated", "creative concept",

    # Vibes/mood spam — subjective feelings, not visual attributes
    "vibes", "vibe", "aesthetic vibes", "chill vibes", "vacation vibes", "mood vibes",
    "feeling", "emotion", "atmosphere", "mood", "ambiance", "ambience",
    "serene", "peaceful", "joyful", "cheerful", "relaxed", "confident", "genuine",
    "wellness", "relaxation", "harmony", "tranquil", "calm", "zen",
    "happy feeling", "positive energy", "good vibes", "cozy feeling",
    "serene atmosphere", "peaceful atmosphere", "calm atmosphere",
    "joyful expression", "happy expression", "genuine expression",
    "confident pose", "relaxed pose", "natural pose",

    # Usage tags that belong in separate field, not SEO keywords
    "magazine editorial spread", "website header banner", "blog feature image",
    "mobile app interface", "brochure print design", "social media post",
    "instagram post", "facebook cover", "twitter header", "pinterest pin",
}


# English-only stock keyword controls
KEYWORD_STOP_WORDS = {
    "a", "an", "the", "and", "or", "for", "of", "in", "on", "at", "to",
    "by", "with", "from", "as", "is", "it", "its", "be", "was", "are",
    "this", "that", "these", "those", "can", "will", "has", "have", "into",
    "over", "under", "near", "up", "down", "out", "off",
}

_PROMPT_STYLE_BLACKLIST = {
    "best quality", "high quality", "ultra detailed", "award winning", "masterpiece",
    "perfect for", "ideal for", "suitable for", "prompt", "metadata", "keyword list",
    "search terms", "seo optimized", "copy and paste", "high resolution image",
    "royalty free image", "stock photo", "stock image", "ai generated", "computer generated",
}

_GENERIC_BUSINESS_PHRASES = {
    "commercial use", "business concept", "marketing campaign", "brand identity",
    "brand visual", "visual content", "content creation", "digital marketing",
    "creative concept", "design inspiration", "abstract concept", "social media content",
}

_GENERIC_FILLER_WORDS = {
    "beautiful", "stunning", "amazing", "gorgeous", "wonderful", "breathtaking",
    "serene", "peaceful", "joyful", "cheerful", "relaxed", "calm", "tranquil",
    "mood", "vibe", "vibes", "emotion", "feeling", "atmosphere", "ambience",
    "wellness", "harmony", "inspiration", "symbolic", "symbolism", "conceptual",
}

_STOCK_USE_CASE_MARKERS = frozenset({
    "banner", "poster", "listing", "campaign", "advertisement", "advertising",
    "brochure", "catalog", "catalogue", "flyer", "header", "thumbnail",
    "template", "mockup", "presentation", "promotional", "branding", "product",
    "display", "ecommerce", "retail", "commercial use",
})

_WEAK_DUPLICATE_TOKENS = frozenset({
    "photo", "image", "picture", "shot", "view", "scene", "copy", "space",
    "commercial", "stock", "background", "backdrop",
})

_SINGULAR_EXCEPTIONS = frozenset({"glass", "grass", "citrus", "series", "species", "news"})


def build_prompt(mode="📷 General Photography", language="en"):
    if mode == "🎨 Commercial Background":
        prompt = _prompt_commercial_background()
    else:
        prompt = _prompt_general_photography()
    if normalize_language(language) == "zh":
        prompt = (prompt.replace("English only", "Simplified Chinese only")
                  .replace("English stock title", "Simplified Chinese stock title")
                  .replace("English stock-search", "Simplified Chinese stock-search")
                  .replace("English keywords", "Simplified Chinese keywords")
                  .replace("English sentences", "Simplified Chinese sentences")
                  .replace("English phrase", "Simplified Chinese phrase")
                  .replace("8 to 12 words", "8 to 30 Chinese characters")
                  .replace("30 to 50 words", "30 to 120 Chinese characters"))
        prompt += "\nOUTPUT LANGUAGE: Use natural Simplified Chinese for title, description and every keyword, suitable for Chinese-language stock search. Keep JSON keys unchanged. Use Chinese sentence punctuation. Do not mix English translations into the keyword list.\n"
    return prompt + """
VISIBLE EVIDENCE CHECK
- Inspect the image before naming its subject. For a close crop, distinguish what is actually identifiable from a merely similar-looking material. Color and parallel bands alone do not prove wood, cement, sand, snow or water. Look for diagnostic structure, edges, joins, reflections and context; when inconclusive describe the visible surface, pattern and light without asserting a material.
- Cover independent visible search dimensions: subject/category, meaningful parts or objects, texture/pattern, distinct colors, light/reflection, spatial arrangement and framing. The target is 25 to 30 RETAINED keywords after factual review and semantic deduplication, with a hard acceptance minimum of 20; a rich scene can justify up to 35 distinct candidates. Do not manufacture dimensions or substitute synonyms for missing evidence. If the image cannot support the acceptance minimum, return only defensible terms for an explicitly unsuccessful quality review, never filler.
- Before finalizing the first 10 keywords, check for a distinctive visible accessory, clothing item, interacting object or identifying visual detail. Give those image-defining details priority over generic composition and category labels; do not omit them merely because the main subject already has several tags.
- Do not count bare frame labels such as foreground, background, image detail or visible object as useful keywords. They are true of almost any image. Use image-specific subject terms and established useful composition phrases instead; do not turn frame positions into filler to reach 20.
- Distinguish illustration, render, and photograph; do not describe a drawing as an actual photographed event.
- Do not guess a character's identity, franchise, exact age, named venue, date, dynasty, or brand.
- Do not mix different national or regional traditions as synonyms. Use broader, defensible terms when attribution is uncertain.
- Keywords must agree with the visible subject and the title and description, including cultural attribution.
- An illustration style alone does not prove AI origin. Set ai_generated only with explicit visible evidence of AI origin; use illustration_or_render for illustrated content.
- Recount the title and description words before returning JSON. Do not pad keyword count with invented subjects or speculative cultural labels.
"""


def build_metadata_repair_prompt(metadata, issues, mode=PROMPT_MODES[0], language=None):
    """One bounded correction using the original image and accepted metadata."""
    return (
        build_prompt(mode, language or metadata_language(metadata))
        + "\nCORRECTION PASS\nFix these specific defects: " + "; ".join(issues)
        + "\nPreserve accurate content and accepted keywords. Do not invent facts to meet word counts."
        + "\nReturn the full metadata JSON using the language and length constraints above."
        + "\nCurrent metadata:\n" + json.dumps(metadata, ensure_ascii=False)
    )



def _prompt_general_photography():
    return _apply_keyword_policy_prompt("""You are a senior stock metadata editor writing in English only for Getty Images, iStock, Shutterstock, Adobe Stock, Alamy, and Freepik.
Your job is to produce stable, buyer-searchable metadata for a single image.

Return ONLY valid JSON:
{"title": "...", "description": "...", "keywords": ["tag1", "tag2", ..., "tag35"], "compliance": {"people": false, "visible_logo": false, "copyrighted_art": false, "private_property": false, "editorial_context": false, "ai_generated": false, "illustration_or_render": false, "mature_content": false}}

COMPLIANCE
- Set each flag from visible evidence only; do not claim that a release or legal clearance exists
- Mark people for recognizable people, visible_logo for logos or trademarks, and copyrighted_art for artwork or characters
- Mark private_property for recognizable private interiors or property, and editorial_context for newsworthy events or public figures
- Mark AI, illustration/render, and mature-content flags when visibly applicable

TITLE
- 8 to 12 words
- Natural English stock title, led by the strongest visible subject
- No hype, no brand slogans, no prompt language

DESCRIPTION
- 30 to 50 words
- 2 or 3 plain English sentences
- Describe only visible content, useful visual details, and at most one realistic stock-use angle

KEYWORDS
- Exactly 35 English keywords
- Order by image-specific relevance. Put the strongest visible subject, its action or defining attribute, and the terms from the title in the first 10 positions
- Build outward from specific subject to meaningful broader category, visible supporting objects, setting, and composition; omit categories that add no useful search meaning
- Prefer individual concepts; retain established multiword subjects such as "coffee cup" or "home office". Do not repeat every adjective-subject combination
- Prioritize visible subject, specific object type, setting, composition, color, lighting, and realistic search variants
- Keep them short, searchable, and commercially useful
- Prefer concrete stock phrases a buyer would actually type
- Remove duplicates, near-duplicates, empty business jargon, mood spam, and prompt-like phrasing
- Do not keyword invisible concepts, symbolic meaning, camera specs, or generic praise
- Limit use-case phrases to two maximum, only when they are natural and searchable

QUALITY TEST
For every keyword ask: would a real buyer search this exact English phrase to license this image on a major stock platform?
If not, do not include it.
""")



def _prompt_commercial_background():
    return _apply_keyword_policy_prompt("""You are a commercial stock metadata editor writing in English only for Adobe Stock, Shutterstock, Getty Images, iStock, Freepik, Etsy, and Creative Market.
Evaluate the image as a possible background, backdrop, texture, or product-display scene. The selected mode does not prove that it has empty space or is suitable for product display. Describe the actual image even if it is cluttered or the material is ambiguous.

Return ONLY valid JSON:
{"title": "...", "description": "...", "keywords": ["tag1", "tag2", ..., "tag35"], "compliance": {"people": false, "visible_logo": false, "copyrighted_art": false, "private_property": false, "editorial_context": false, "ai_generated": false, "illustration_or_render": false, "mature_content": false}}

COMPLIANCE
- Set each flag from visible evidence only; do not claim that a release or legal clearance exists
- Mark visible people, logos/trademarks, artwork/characters, recognizable private property, or editorial context
- Mark AI-generated, illustration/render, and mature-content flags when visibly applicable

TITLE
- 8 to 12 words
- Focus on the visible surface, identifiable material, color, and composition; do not force a display context
- Keep it evergreen; never force seasonal terms unless clearly visible

DESCRIPTION
- 30 to 50 words
- 2 or 3 plain English sentences
- Explain the visible backdrop, surface quality, color palette and lighting. Mention product-display use only when the visible composition supports it
- Keep it factual and commercially grounded

KEYWORDS
- Exactly 35 English keywords
- Order by image-specific relevance. Put the defining backdrop, texture, material, and genuinely distinctive visual attributes in the first 10 positions
- Prefer individual concepts and established multiword search phrases. Avoid permutations of the same color, material, and background words
- Start with the visible backdrop or texture itself
- Expand with natural buyer-search variants, material terms, color terms, composition terms, and realistic product-display phrases
- Keep phrases searchable for Getty, Shutterstock, Adobe Stock, and iStock
- Avoid filler such as mood words, empty business jargon, symbolic language, and generic prompt phrasing
- Use at most two commercial use-case phrases
- Do not pad with weak platform, branding, or marketing buzzwords

QUALITY TEST
Every keyword must feel like a real search term for licensing a stock background asset, not a model prompt and not internal taxonomy.
""")



def _normalize_keyword_text(keyword):
    value = str(keyword or "").strip().strip('"').strip("'").strip()
    value = value.replace("/", " ").replace("_", " ").replace("-", " ")
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" ,;")
    return value


def _parse_batch_keyword_input(value):
    """Parse comma, semicolon, or line separated user keywords."""
    return [
        keyword
        for keyword in (_normalize_keyword_text(raw) for raw in re.split(r"[;,；，、\n]+", str(value or "")))
        if keyword
    ]


def _merge_keyword_lists(existing, additions):
    """Append normalized additions, preserving existing order and exact text."""
    existing_values = list(existing or [])
    merged = []
    seen = set()
    for index, raw in enumerate(existing_values + list(additions or [])):
        normalized = _normalize_keyword_text(raw)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        merged.append(str(raw).strip() if index < len(existing_values) else normalized)
    return merged



def _keyword_tokens(keyword):
    return re.findall(r"[a-z0-9]+", str(keyword or "").lower())



def _stem_keyword_token(token):
    if token in _SINGULAR_EXCEPTIONS or len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ses", "xes", "zes")) and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token



def _keyword_signature_tokens(keyword):
    return tuple(
        _stem_keyword_token(token)
        for token in _keyword_tokens(keyword)
        if token not in KEYWORD_STOP_WORDS
    )



def _contains_non_english_text(keyword):
    return bool(re.search(r"[^\x00-\x7F]", str(keyword or "")))



def _looks_prompt_like_keyword(keyword_lower):
    return any(phrase in keyword_lower for phrase in _PROMPT_STYLE_BLACKLIST)



def _looks_empty_business_keyword(signature_tokens):
    if not signature_tokens:
        return True
    phrase = " ".join(signature_tokens)
    if phrase in _GENERIC_BUSINESS_PHRASES:
        return True
    business_only = {"commercial", "business", "marketing", "brand", "branding", "digital", "content", "creative"}
    return set(signature_tokens).issubset(business_only)



def _looks_filler_heavy(keyword_lower, signature_tokens):
    if keyword_lower in KEYWORD_BLACKLIST:
        return True
    if keyword_lower in _GENERIC_BUSINESS_PHRASES:
        return True
    filler_hits = [token for token in signature_tokens if token in _GENERIC_FILLER_WORDS]
    if len(signature_tokens) <= 2 and filler_hits:
        return True
    if signature_tokens and len(filler_hits) >= max(1, len(signature_tokens) - 1):
        return True
    return False



def _is_near_duplicate(signature_tokens, existing_signature_tokens):
    left = set(signature_tokens)
    right = set(existing_signature_tokens)
    if not left or not right:
        return False
    if left == right:
        return True
    overlap = len(left & right) / max(len(left), len(right))
    if overlap >= 0.85 and (left ^ right).issubset(_WEAK_DUPLICATE_TOKENS):
        return True
    if left < right and (right - left).issubset(_WEAK_DUPLICATE_TOKENS):
        return True
    if right < left and (left - right).issubset(_WEAK_DUPLICATE_TOKENS):
        return True
    return False



# ── Stock sanitizer constants ──
# Below the minimum, attempt one keyword repair. The recommended count is a
# generation target, not a quality quota. Export supports up to 49 useful tags.
KEYWORD_POLICY = {
    "minimum": 5,
    "recommended": 35,
    "maximum": 49,
}
KEYWORD_MINIMUM_COUNT = KEYWORD_POLICY["minimum"]
KEYWORD_TARGET_COUNT = KEYWORD_POLICY["recommended"]
KEYWORD_MAXIMUM_COUNT = KEYWORD_POLICY["maximum"]
GENERATED_KEYWORD_MINIMUM = 20
GENERATED_KEYWORD_MAXIMUM = 35
# Bare labels true of nearly any frame do not add a precise buyer search.
# Named subjects/patterns and useful established composition phrases remain.
_EMPTY_FRAME_LABELS = frozenset({"foreground", "background", "image detail", "visible object", "前景", "背景", "画面细节", "可见物体"})


def keyword_limits(data):
    if data.get("keyword_policy_version") == 2:
        return 10, GENERATED_KEYWORD_MAXIMUM
    if data.get("keyword_policy_version") == 3:
        return GENERATED_KEYWORD_MINIMUM, GENERATED_KEYWORD_MAXIMUM
    return KEYWORD_MINIMUM_COUNT, KEYWORD_MAXIMUM_COUNT


def _apply_keyword_policy_prompt(prompt):
    return str(prompt).replace('["tag1", "tag2", ..., "tag35"]', '["main subject", "distinct visible detail"]').replace(
        "Exactly 35 English keywords",
        "Draft 25 to 35 English keywords supported by the image, aiming for about 30 independently useful candidates when the scene allows. This response is a DRAFT, not the final audited list. Final acceptance is 20 to 35 distinct keywords. Target 25 to 30 retained keywords AFTER sanitization, semantic deduplication and visual review; uncertain words do not count. Do not aim to fill 35 slots. If the image cannot support the draft range, return only defensible concepts for an unsuccessful quality review rather than fabricate. Never pad with weak or invented tags to satisfy a numeric minimum. Select in layers: core subject or backdrop; distinguishing visible attributes; useful supporting objects, setting and composition; at most two directly supported use cases. Omit layers without useful evidence and never assign layer quotas"
    ) + (
        "\nSEARCH-VALUE CHECK: Choose one preferred term for equivalent synonyms. Do not repeat "
        "one subject as a chain of alternate names or color/count/pose permutations. A broader "
        "category is useful only when it adds a distinct buyer search, not another quota slot. "
        "Useful ordinary subject categories and conventional search themes may be included without "
        "claiming a hidden event actually occurred. Keep factual captions stricter: do not turn a "
        "search theme into an unobserved event, time, ingredient or provenance assertion. "
        "Do not infer a country from vehicle or architectural styling. Do not name a sauce, "
        "ingredient, cooking method, animal species or breed unless distinctive visible evidence "
        "establishes it; use the broader visible category when uncertain. For example, a yellow "
        "sauce is not proof of mustard. Before returning JSON, delete redundant and speculative "
        "terms even if that leaves fewer keywords. These examples are instructions, not tags to copy."
    )

_GENERAL_SUBJECT_TOKENS = {
    "person", "people", "woman", "women", "man", "men", "child", "children",
    "girl", "boy", "baby", "family", "couple", "adult", "senior", "worker",
    "student", "doctor", "animal", "bird", "dog", "cat",
}
_PEOPLE_TOKENS = {
    "person", "people", "woman", "women", "man", "men", "child", "children",
    "girl", "boy", "baby", "family", "couple", "adult", "senior", "worker",
    "student", "doctor",
}
_SETTING_TOKENS = {
    "indoor", "outdoor", "office", "home", "room", "studio", "kitchen", "street",
    "city", "urban", "rural", "forest", "beach", "ocean", "mountain", "garden",
    "workplace", "restaurant", "school", "hospital", "nature",
}
_COMPOSITION_PHRASES = {
    "close up", "copy space", "top view", "side view", "front view", "low angle",
    "high angle", "selective focus", "shallow depth", "portrait orientation",
    "landscape orientation", "isolated", "panoramic", "macro", "flat lay",
}
_COLOR_MATERIAL_TOKENS = {
    "red", "orange", "yellow", "green", "blue", "purple", "pink", "white", "black",
    "gray", "grey", "brown", "gold", "silver", "wood", "wooden", "metal", "metallic",
    "glass", "paper", "plastic", "fabric", "textile", "stone", "marble", "concrete",
    "leather", "ceramic", "cotton", "silk",
}
_BACKGROUND_TOKENS = {"background", "backdrop", "texture", "pattern", "surface", "wall", "floor"}


def _keyword_priority(keyword, mode="📷 General Photography"):
    normalized = _normalize_keyword_text(keyword).lower()
    is_use_case = (
        any(marker in normalized for marker in _STOCK_USE_CASE_MARKERS)
        or normalized in _GENERIC_BUSINESS_PHRASES
        or any(marker in normalized for marker in _CHINESE_USE_CASES)
    )
    # A token table cannot tell whether a mountain is the subject or the setting.
    # Preserve the image-grounded order supplied by generation or manual editing.
    # Explicit marketing/use terms remain behind the visual subject terms.
    return 5 if is_use_case else 0


def rank_keywords_for_stock(keywords, mode="📷 General Photography"):
    """Preserve visual relevance order, moving explicit use cases to the end."""
    return [
        keyword
        for _priority, _index, keyword in sorted(
            (_keyword_priority(keyword, mode), index, keyword)
            for index, keyword in enumerate(keywords or [])
        )
    ]


_CHINESE_FILLER = frozenset({
    "精美", "美丽", "漂亮", "惊艳", "高质量", "高清", "超高清", "最佳质量",
    "图库图片", "素材图片", "版权图片", "免版税", "完美", "关键词", "关键词列表",
    "商业概念", "设计灵感", "品牌营销", "营销概念", "提示词", "元数据",
})
_CHINESE_USE_CASES = ("广告", "海报", "横幅", "产品展示", "宣传册", "电商展示", "样机")


def _chinese_search_signature(value):
    # Only spelling/wording variants with the same search meaning. Do not
    # collapse a compound subject into its head noun (coffee vs coffee cup).
    value = re.sub(r"^盛(.{1,6})的杯子?$", r"\1杯", value)
    value = re.sub(r"杯子$", "杯", value)
    value = value.replace("木制", "木质")
    return {"乳牛": "奶牛", "乳牛场": "奶牛场"}.get(value, value)


def _sanitize_chinese_keywords(keywords, maximum, keyword_policy_version=None):
    kept, rejected, seen = [], [], set()
    use_cases = 0
    for raw in keywords or []:
        if not isinstance(raw, str):
            rejected.append((raw, "keyword must be text"))
            continue
        value = unicodedata.normalize("NFKC", raw).strip().strip("\"'，；,;、。")
        value = re.sub(r"\s+", "", value)
        if not value:
            continue
        key = _chinese_search_signature(value.casefold())
        if not re.search(r"[\u3400-\u9fff]", value):
            rejected.append((raw, "non-Chinese keyword"))
        elif not re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9·]+", value) or len(value) > 16:
            rejected.append((raw, "not a short Chinese search phrase"))
        elif key in seen:
            rejected.append((raw, "duplicate"))
        elif (keyword_policy_version == 3 and value in _EMPTY_FRAME_LABELS) or value in _CHINESE_FILLER or any(term in value for term in ("最佳质量", "超高清", "提示词", "关键词列表")):
            rejected.append((raw, "filler or prompt-style phrase"))
        elif any(term in value for term in _CHINESE_USE_CASES) and use_cases >= 2:
            rejected.append((raw, "too many use-case keywords"))
        else:
            kept.append(value)
            seen.add(key)
            use_cases += int(any(term in value for term in _CHINESE_USE_CASES))
    if len(kept) > maximum:
        rejected.extend((value, "over target count") for value in kept[maximum:])
        kept = kept[:maximum]
    return kept, rejected, max(0, maximum - len(kept))


def sanitize_keywords_for_stock(keywords, target_count=KEYWORD_TARGET_COUNT, language="en", keyword_policy_version=None):
    """Normalize AI keywords for English stock-photo search quality.

    Returns (kept, rejected, shortfall) where rejected items are (keyword, reason).
    """
    kept = []
    rejected = []
    seen_normalized = set()
    seen_signatures = []
    prefix_counts = {}
    use_case_count = 0

    if normalize_language(language) == "zh":
        return _sanitize_chinese_keywords(keywords, target_count, keyword_policy_version)

    for raw in keywords or []:
        if not isinstance(raw, str):
            rejected.append((raw, "keyword must be text"))
            continue
        normalized = _normalize_keyword_text(raw)
        if not normalized:
            continue
        normalized_lower = normalized.lower()
        if keyword_policy_version == 3 and normalized_lower in _EMPTY_FRAME_LABELS:
            rejected.append((raw, "empty framing label without independent search value"))
            continue
        signature_tokens = _keyword_signature_tokens(normalized)
        tokens = _keyword_tokens(normalized)

        if _contains_non_english_text(normalized):
            rejected.append((raw, "non-English keyword"))
            continue
        if not tokens:
            rejected.append((raw, "empty keyword"))
            continue
        if normalized_lower in seen_normalized:
            rejected.append((raw, "duplicate"))
            continue
        if _looks_prompt_like_keyword(normalized_lower):
            rejected.append((raw, "prompt-style phrase"))
            continue
        if tokens[-1] in {"image", "photo", "picture"}:
            rejected.append((raw, "generic media suffix"))
            continue
        if tokens[-1] in {"detail", "details"}:
            rejected.append((raw, "generic detail suffix"))
            continue
        if _looks_filler_heavy(normalized_lower, signature_tokens):
            rejected.append((raw, "filler or mood spam"))
            continue
        if _looks_empty_business_keyword(signature_tokens):
            rejected.append((raw, "empty business jargon"))
            continue
        if len(tokens) > 5:
            rejected.append((raw, "too long for stock search"))
            continue

        duplicate_match = None
        for existing_keyword, existing_signature in seen_signatures:
            if _is_near_duplicate(signature_tokens, existing_signature):
                duplicate_match = existing_keyword
                break
        if duplicate_match is not None:
            rejected.append((raw, f"near-duplicate of '{duplicate_match}'"))
            continue

        first_token = signature_tokens[0] if signature_tokens else tokens[0]
        # Older/manual records keep the conservative prefix cap. New generated
        # records are separately checked by the model for semantic duplicates;
        # a fixed cap of three incorrectly discarded distinct facets such as
        # cloud color, texture, edges and lighting before that audit ran.
        prefix_limit = 2 if first_token in {"background", "backdrop", "product", "commercial", "business", "marketing"} else 3
        if keyword_policy_version != 3 and prefix_counts.get(first_token, 0) >= prefix_limit:
            rejected.append((raw, f"prefix repetition ('{first_token}')"))
            continue

        if any(marker in normalized_lower for marker in _STOCK_USE_CASE_MARKERS):
            if use_case_count >= 2:
                rejected.append((raw, "too many use-case keywords"))
                continue
            use_case_count += 1

        kept.append(normalized)
        seen_normalized.add(normalized_lower)
        seen_signatures.append((normalized, signature_tokens))
        prefix_counts[first_token] = prefix_counts.get(first_token, 0) + 1

    if len(kept) > target_count:
        for extra in kept[target_count:]:
            rejected.append((extra, "over target count"))
        kept = kept[:target_count]

    shortfall = max(0, target_count - len(kept))
    return kept, rejected, shortfall



def clean_keywords(keywords):
    kept, _rejected, _shortfall = sanitize_keywords_for_stock(keywords, target_count=KEYWORD_TARGET_COUNT)
    return kept


_KW_REVIEW_BLACKLIST = {
    "vibes", "vibe", "mood", "feeling", "atmosphere", "ambiance", "ambience",
    "beautiful", "stunning", "amazing", "gorgeous", "stock photo", "stock image",
    "business concept", "creative concept", "design inspiration", "perfect for",
}



def validate_keywords(keywords, language="en", generated=False, keyword_policy_version=None):
    """Return list of quality issues found in the keyword set."""
    issues = []
    minimum, maximum = keyword_limits({"keyword_policy_version": keyword_policy_version or 2} if generated else {})
    if len(keywords) < minimum:
        issues.append(f"Only {len(keywords)} keywords (expected at least {minimum})")
    elif len(keywords) > maximum:
        issues.append(f"Too many keywords: {len(keywords)} (maximum {maximum})")
    for kw in keywords:
        if not isinstance(kw, str):
            issues.append("Keywords must contain text values only")
            continue
        kw_lower = kw.lower()
        if keyword_policy_version == 3 and kw_lower in _EMPTY_FRAME_LABELS:
            issues.append(f"Weak keyword '{kw}' has no independent search value")
            continue
        if normalize_language(language) == "zh":
            if not re.search(r"[\u3400-\u9fff]", kw):
                issues.append(f"Non-Chinese keyword: {kw}")
            continue
        if _contains_non_english_text(kw):
            issues.append(f"Non-English keyword: {kw}")
            continue
        if _looks_prompt_like_keyword(kw_lower):
            issues.append(f"Prompt-like keyword: {kw}")
            continue
        for banned in _KW_REVIEW_BLACKLIST:
            if banned in kw_lower:
                issues.append(f"Weak keyword '{banned}' in: {kw}")
                break
    return issues


def _stock_word_count(value):
    return len(re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*", str(value or "")))


_COMPLIANCE_KEYS = (
    "people", "visible_logo", "copyrighted_art", "private_property",
    "editorial_context", "ai_generated", "illustration_or_render", "mature_content",
)


_CULTURAL_CONTEXTS = {
    "Chinese": frozenset({"chinese", "china", "sichuan", "beijing", "peking"}),
    "Japanese": frozenset({"japanese", "japan", "kabuki", "torii"}),
    "Korean": frozenset({"korean", "korea", "hanbok"}),
}
_CHINESE_CULTURAL_CONTEXTS = {
    "Chinese": ("中国", "中华", "中式", "京剧", "川剧", "昆曲", "中国戏曲"),
    "Japanese": ("日本", "日式", "歌舞伎", "鸟居", "和服"),
    "Korean": ("韩国", "朝鲜", "韩服", "韩式"),
}


def _cultural_contexts(text):
    tokens = set(_keyword_tokens(text))
    return {name for name, markers in _CULTURAL_CONTEXTS.items()
            if tokens & markers or any(marker in text for marker in _CHINESE_CULTURAL_CONTEXTS[name])}


def metadata_context_warnings(title, description, keywords):
    """Flag conflicting cultural labels for review, without claiming image understanding.

    A record can legitimately contain several cultures. Only flag keywords
    introducing a different culture when the text identifies exactly one.
    The user can still keep such keywords after checking the image.
    """
    contexts = _cultural_contexts(f"{title or ''} {description or ''}")
    if len(contexts) != 1:
        return []
    warnings = []
    for keyword in keywords or []:
        for name in _cultural_contexts(str(keyword)):
            if name not in contexts:
                warnings.append(f"Potential keyword context mismatch: '{keyword}' adds {name} attribution absent from title and description")
    return warnings


def normalize_compliance_flags(value):
    source = value if isinstance(value, dict) else {}
    return {key: source.get(key) is True for key in _COMPLIANCE_KEYS}


def validate_metadata_mode(title, description, keywords, mode):
    combined = " ".join([str(title or ""), str(description or ""), *map(str, keywords or [])]).lower()
    tokens = set(_keyword_tokens(combined))
    background_hits = tokens & _BACKGROUND_TOKENS
    supporting_background = bool(tokens & _COLOR_MATERIAL_TOKENS) or any(
        phrase in combined for phrase in _COMPOSITION_PHRASES
    )
    commercial_use = any(marker in combined for marker in _STOCK_USE_CASE_MARKERS)
    if re.search(r"[\u3400-\u9fff]", combined):
        background_hits |= {term for term in ("背景", "底纹", "纹理", "图案", "表面") if term in combined}
        supporting_background = supporting_background or any(term in combined for term in (
            "红色", "蓝色", "绿色", "白色", "黑色", "金色", "灰色", "紫色", "木", "金属", "石", "布", "纸", "留白", "俯视", "光线", "材质",
        ))
        commercial_use = commercial_use or any(term in combined for term in _CHINESE_USE_CASES)

    if mode == "🎨 Commercial Background":
        if not background_hits or not supporting_background:
            return ["Commercial Background mode does not match the visible metadata concepts"]
    elif len(background_hits) >= 2 and commercial_use:
        return ["General Photography mode appears to describe a commercial background asset"]
    return []


def metadata_compliance_warnings(title, description, keywords, compliance=None):
    flags = normalize_compliance_flags(compliance)
    combined = " ".join([str(title or ""), str(description or ""), *map(str, keywords or [])]).lower()
    tokens = set(_keyword_tokens(combined))

    people_detected = flags["people"] or bool(tokens & _PEOPLE_TOKENS)
    logo_detected = flags["visible_logo"] or bool(tokens & {"logo", "trademark", "brand"})
    art_detected = flags["copyrighted_art"] or bool(tokens & {"artwork", "mural", "sculpture", "character"})
    property_detected = flags["private_property"] or any(
        phrase in combined for phrase in ("private property", "private interior", "recognizable property")
    )
    editorial_detected = flags["editorial_context"] or bool(
        tokens & {"editorial", "news", "celebrity", "protest", "concert", "demonstration"}
    )
    ai_detected = flags["ai_generated"] or any(
        phrase in combined for phrase in ("ai generated", "generative ai", "synthetic image")
    )
    illustration_detected = flags["illustration_or_render"] or any(
        phrase in combined for phrase in ("3d render", "digital illustration", "digital art")
    )
    mature_detected = flags["mature_content"] or bool(tokens & {"nude", "nudity", "sexual", "erotic"})
    # These are conservative review hints, never assertions of ownership or
    # legal clearance. The model's explicit flags remain authoritative inputs.
    people_detected |= any(term in combined for term in ("人物", "人像", "男人", "女人", "男孩", "女孩", "儿童", "婴儿", "老人", "情侣", "医生", "工人"))
    logo_detected |= any(term in combined for term in ("商标", "品牌标志", "品牌标识", "企业标志"))
    art_detected |= any(term in combined for term in ("壁画", "雕塑", "雕像", "动漫角色", "版权角色", "艺术作品"))
    property_detected |= any(term in combined for term in ("私人室内", "私人住宅", "私人建筑", "私有财产"))
    editorial_detected |= any(term in combined for term in ("新闻事件", "示威", "抗议", "公众人物", "演唱会"))
    ai_detected |= any(term in combined for term in ("ai生成", "ai生成图", "人工智能生成", "生成式ai"))
    illustration_detected |= any(term in combined for term in ("数字插画", "数字艺术", "三维渲染", "3d渲染"))
    mature_detected |= any(term in combined for term in ("裸体", "裸露", "色情", "情色"))

    warnings = []
    if people_detected:
        warnings.append("People detected: verify model releases and editorial requirements")
    if logo_detected:
        warnings.append("Logo or trademark risk detected: verify commercial eligibility")
    if art_detected:
        warnings.append("Artwork or character risk detected: verify copyright clearance")
    if property_detected:
        warnings.append("Private property risk detected: verify property release requirements")
    if editorial_detected:
        warnings.append("Editorial context detected: verify caption, date, location, and editorial status")
    if ai_detected:
        warnings.append("AI-generated content indicated: apply the destination platform AI disclosure")
    if illustration_detected:
        warnings.append("Illustration or render indicated: verify the platform content-type field")
    if mature_detected:
        warnings.append("Mature content indicated: verify age, consent, and mature-content settings")
    return warnings


def validate_stock_metadata(title, description, keywords, mode="📷 General Photography", language=None, keyword_language=None, generated=False, keyword_policy_version=None):
    """Return blocking delivery issues for one stock metadata record."""
    issues = []
    title = str(title or "").strip()
    description = str(description or "").strip()
    keyword_list = list(keywords) if isinstance(keywords, (list, tuple)) else []
    language = normalize_language(language) if language else metadata_language({"title": title, "description": description})

    title_words = _stock_word_count(title)
    if language == "zh":
        title_words = len(re.findall(r"[\u3400-\u9fff]", title))
        if not 8 <= title_words <= 30:
            issues.append(f"Title has {title_words} Chinese characters (expected 8-30)")
    elif not 8 <= title_words <= 12:
        issues.append(f"Title has {title_words} words (expected 8-12)")
    if language == "en" and _contains_non_english_text(title):
        issues.append("Title must use English text only")

    description_words = _stock_word_count(description)
    if language == "zh":
        description_words = len(re.findall(r"[\u3400-\u9fff]", description))
        if not 30 <= description_words <= 120:
            issues.append(f"Description has {description_words} Chinese characters (expected 30-120)")
    elif not 30 <= description_words <= 50:
        issues.append(f"Description has {description_words} words (expected 30-50)")
    sentence_pattern = r"[^。！？.!?]+[。！？.!?]+" if language == "zh" else r"[^.!?]+[.!?]+(?:\s|$)"
    sentence_count = len(re.findall(sentence_pattern, description))
    if not 2 <= sentence_count <= 3:
        issues.append(f"Description has {sentence_count} sentences (expected 2-3)")
    if language == "en" and _contains_non_english_text(description):
        issues.append("Description must use English text only")

    keyword_language = normalize_language(keyword_language or language)
    issues.extend(validate_keywords(keyword_list, keyword_language, generated, keyword_policy_version))
    sanitized, rejected, shortfall = sanitize_keywords_for_stock(
        keyword_list, target_count=KEYWORD_MAXIMUM_COUNT, language=keyword_language,
        keyword_policy_version=keyword_policy_version,
    )
    normalized_input = [_normalize_keyword_text(keyword) for keyword in keyword_list]
    if rejected or sanitized != normalized_input:
        issues.append("Keywords must pass stock sanitization without removals or replacements")
    if sanitized and sanitized != rank_keywords_for_stock(sanitized, mode):
        issues.append("Keywords are not ordered by stock-search priority")
    issues.extend(validate_metadata_mode(title, description, keyword_list, mode))
    issues.extend(metadata_context_warnings(title, description, keyword_list))
    return list(dict.fromkeys(issues))


def classify_stock_metadata(title, description, keywords, mode="📷 General Photography", language=None, keyword_language=None, generated=False, keyword_policy_version=None):
    """Split blocking metadata defects from fixable keyword quality reviews."""
    keyword_list = list(keywords) if isinstance(keywords, (list, tuple)) else []
    issues = validate_stock_metadata(title, description, keyword_list, mode, language, keyword_language, generated, keyword_policy_version)
    hard_errors = []
    review_warnings = []
    keyword_review_prefixes = (
        "Only ",
        "Too many keywords:",
        "Non-English keyword:",
        "Non-Chinese keyword:",
        "Prompt-like keyword:",
        "Weak keyword ",
        "Keywords must pass",
        "Keywords are not ordered",
        "Potential keyword context mismatch:",
    )
    for issue in issues:
        if issue.startswith(keyword_review_prefixes):
            review_warnings.append(issue)
        else:
            hard_errors.append(issue)
    return list(dict.fromkeys(hard_errors)), list(dict.fromkeys(review_warnings))
