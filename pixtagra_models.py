"""Model catalog, provider settings and response decoding without GUI state.

The model catalog is the single source for both requests and UI choices.
"""

import json
import re


OPENAI_GPT_56_CANDIDATES = (
    ("OpenAI GPT-5.6 Sol", "gpt-5.6-sol"),
    ("OpenAI GPT-5.6 Terra", "gpt-5.6-terra"),
    ("OpenAI GPT-5.6 Luna", "gpt-5.6-luna"),
)


# Compatibility-only: never reinterpret an already saved ID as another model.
# These mistaken presets are hidden from new choices, but old settings continue
# to resolve to exactly their saved ID (including any explicit override).
LEGACY_GPT_56_SELECTIONS = (
    ("OpenAI GPT-5.6", "gpt-5.6"),
    ("OpenAI GPT-5.6 mini", "gpt-5.6-mini"),
    ("OpenAI GPT-5.6 nano", "gpt-5.6-nano"),
)


def migrate_model_selection(name):
    """Retain the provider of a saved, removed GPT selection during upgrades."""
    if name in {display_name for display_name, _model_id in (*OPENAI_GPT_56_CANDIDATES, *LEGACY_GPT_56_SELECTIONS)}:
        return name
    if isinstance(name, str) and name.startswith("OpenAI GPT-"):
        return "OpenAI GPT-5.5"
    return name


def configured_base_urls(config):
    """Read provider-scoped endpoints, assigning a legacy URL only to its owner."""
    source = config if isinstance(config, dict) else {}
    saved = source.get("provider_base_urls", {})
    urls = {
        provider: value.strip()
        for provider, value in (saved.items() if isinstance(saved, dict) else ())
        if isinstance(provider, str) and isinstance(value, str)
    }
    owner = get_provider_config(migrate_model_selection(source.get("model", ""))).get("provider")
    legacy = source.get("base_url", "")
    if owner not in {"unknown", "ollama"} and owner not in urls and isinstance(legacy, str):
        urls[owner] = legacy.strip()
    return urls


def remember_provider_base_url(config, model_name, base_url):
    """Return updated settings without mutating the caller's snapshot."""
    updated = dict(config) if isinstance(config, dict) else {}
    urls = configured_base_urls(updated)
    provider = get_provider_config(model_name).get("provider")
    if provider not in {"unknown", "ollama"}:
        urls[provider] = str(base_url or "").strip()
    updated["provider_base_urls"] = urls
    return updated


MODEL_LIFECYCLE = {
    # Provider availability can change independently of PixTagra's UI.
    # `recommended` selects the default; it is not a permanent availability claim.
    "OpenAI GPT-5.5": {"release_status": "legacy_saved_only", "recommended": False,
                       "requires_live_check": True, "review_required": False},
    # Terra is the user's chosen default. All candidates still require the
    # selected account's preflight; none is an automatic fallback.
    **{
        display_name: {"release_status": "candidate_requires_live_check", "recommended": display_name == "OpenAI GPT-5.6 Terra",
                       "requires_live_check": True, "review_required": True}
        for display_name, _model_id in OPENAI_GPT_56_CANDIDATES
    },
    "Moonshot Kimi": {"release_status": "experimental", "recommended": False,
                      "requires_live_check": True, "review_required": True},
    "Qwen 3.7 Plus": {"release_status": "current_candidate", "recommended": False,
                      "requires_live_check": True, "review_required": False},
    "MiniMax M3": {"release_status": "current_candidate", "recommended": False,
                   "requires_live_check": True, "review_required": True},
    "Local Ollama": {"release_status": "local_experimental", "recommended": False,
                     "requires_live_check": True, "review_required": True},
}


def _lifecycle_for(name):
    """Return a copy so callers cannot mutate catalog policy."""
    return dict(MODEL_LIFECYCLE.get(name, {
        "release_status": "unknown", "recommended": False,
        "requires_live_check": True, "review_required": True,
    }))


API_CONFIG = {
    # ── Priority Tier 1: Best cost-performance for stock photo metadata ──
    # Gemini 2.5 Flash — FREE tier available, excellent vision, fast
    "Google Gemini 2.5 Flash": {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "stability": "production",
        "fallback_eligible": True,
    },
    # Gemini 2.5 Pro — Best vision quality among free/low-cost options
    "Google Gemini 2.5 Pro": {
        "provider": "gemini",
        "model": "gemini-2.5-pro",
        "stability": "production",
        "fallback_eligible": True,
    },
    # ── Priority Tier 2: Strong quality, moderate cost ──
    # OpenAI catalog and transport mirror the validated AlbumDone provider kit
    # (source commit 97421d7783a7c84025e2abfe76eaa72fe13bac69).
    "OpenAI GPT-5.5": {
        "provider": "openai",
        "url": "https://api.openai.com/v1",
        "model": "gpt-5.5",
        "legacy_saved_only": True,
        "protocol": "responses",
        "stability": "production",
        "fallback_eligible": False,
    },
    **{
        display_name: {
            "provider": "openai",
            "url": "https://api.openai.com/v1",
            "model": model_id,
            "protocol": "responses",
            "stability": "candidate_requires_live_check",
            "legacy_saved_only": (display_name, model_id) in LEGACY_GPT_56_SELECTIONS,
            "fallback_eligible": False,
        }
        for display_name, model_id in (*OPENAI_GPT_56_CANDIDATES, *LEGACY_GPT_56_SELECTIONS)
    },
    # K2.6 is returned by the authenticated official model catalog and is
    # validated with image input; K2.5 now returns model-not-found for this route.
    "Moonshot Kimi": {
        "provider": "kimi",
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "model": "kimi-k2.6",
        "stability": "experimental",
        "fallback_eligible": False,
    },
    "Qwen 3.7 Plus": {
        "provider": "qwen", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.7-plus", "stability": "production", "fallback_eligible": False,
    },
    "MiniMax M3": {
        "provider": "minimax",
        "url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M3",
        "stability": "production",
        "fallback_eligible": False,
    },
    # ── Priority Tier 4: Local / Privacy-first ──
    # Ollama — Free, offline, no API key needed. Requires local GPU.
    "Local Ollama": {
        "provider": "ollama",
        "url": "http://localhost:11434/api/chat",
        "model": "llava",
        "stability": "experimental",
        "fallback_eligible": False,
    }
}

# Keep lifecycle metadata beside the transport catalog. This makes the
# recommendation and review policy inspectable without changing request code.
for _model_name, _model_config in API_CONFIG.items():
    _model_config.update(_lifecycle_for(_model_name))

RECOMMENDED_MODEL = next(
    (name for name, config in API_CONFIG.items() if config.get("recommended")),
    next(iter(API_CONFIG)),
)


# Gemini remains an internal legacy transport for old configuration and
# credential migration only. It is intentionally not exposed to users because
# the shipped Google model IDs are no longer reliably available.
SELECTABLE_MODELS = tuple(
    name for name, config in API_CONFIG.items() if config.get("provider") != "gemini" and not config.get("legacy_saved_only")
)

# ── Model Priority Guide ──
# Tier 2 (Premium): GPT-5.6 Terra is the user-selected default candidate. The
# GPT-5.6 entries are selectable candidates and require the existing live
# connection check before use.
# live preflight remains mandatory because provider availability can change.
#   - Higher quality keywords, better commercial term understanding.
#   - OpenAI: https://platform.openai.com
#
# Tier 3 (Budget): Moonshot Kimi / Qwen / MiniMax
#   - Kimi, Qwen, and MiniMax expose OpenAI-compatible multimodal routes.
#   - Qwen defaults to the provider kit's DashScope compatible-mode endpoint.
#
# Tier 4 (Offline): Local Ollama + LLaVA
#   - Free, fully offline. Quality depends on your GPU (8GB VRAM minimum).
#   - Install: https://ollama.com → run: ollama pull llava
#   - Better alternatives: llava:34b (16GB VRAM) or llama3.2-vision (11B)

# ── Provider metadata for every model the UI exposes ──
# Keys match the exact display names used in the model selection widget.
PROVIDERS = {
    name: {
        "provider": config["provider"],
        "type": "local" if config["provider"] == "ollama" else "cloud",
        "requires_api_key": config["provider"] != "ollama",
        "stability": config["stability"],
        "fallback_eligible": config["fallback_eligible"],
        "release_status": config["release_status"],
        "recommended": config["recommended"],
        "requires_live_check": config["requires_live_check"],
        "review_required": config["review_required"],
        **({"default_base_url": config["url"], "ollama_model": config["model"]}
           if config["provider"] == "ollama" else {}),
    }
    for name, config in API_CONFIG.items()
}


def get_provider_config(model_name: str) -> dict:
    """Return provider config for a model. Defaults to cloud if unknown."""
    return PROVIDERS.get(model_name, {"provider": "unknown", "type": "cloud", "requires_api_key": True})


# Zhipu is deliberately absent from the selectable model catalog.  Keep its
# provider identifier only for an explicit migration of an existing config.
ACTIVE_GUI_CREDENTIAL_PROVIDERS = frozenset(("openai", "kimi", "qwen", "minimax"))
LEGACY_CONFIG_MODEL_PROVIDERS = {
    "Google Gemini 2.5 Flash": "gemini",
    "Google Gemini 2.5 Pro": "gemini",
    "Zhipu GLM-4V": "zhipu",
}


def _legacy_config_provider_id(model_name: str) -> str:
    """Resolve a provider only for explicitly migrating a legacy config key."""
    provider = get_provider_config(migrate_model_selection(model_name)).get("provider", "")
    return provider if provider in ACTIVE_GUI_CREDENTIAL_PROVIDERS else LEGACY_CONFIG_MODEL_PROVIDERS.get(model_name, "")



def extract_json(text, require_object=False):
    if not text:
        return None
    text = re.sub(r'```json?\s*\n?', '', text).strip().rstrip('`')
    try:
        parsed = json.loads(text)
        if require_object and not isinstance(parsed, dict):
            return None
        return parsed
    except Exception:
        pass
    s, e = text.find('{'), text.rfind('}')
    if s != -1 and e > s:
        try:
            parsed = json.loads(text[s:e+1])
            if require_object and not isinstance(parsed, dict):
                return None
            return parsed
        except Exception:
            pass
    return None


def _coerce_text_content(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                elif isinstance(item.get("content"), list):
                    nested = _coerce_text_content(item.get("content"))
                    if nested:
                        parts.append(nested)
        return "\n".join(p for p in parts if p).strip()
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), str):
            return value["content"]
        if isinstance(value.get("content"), list):
            return _coerce_text_content(value["content"])
    return str(value)


def extract_api_text(payload, transport, provider=""):
    if not isinstance(payload, dict):
        return _coerce_text_content(payload)
    if transport == "gemini":
        candidates = payload.get("candidates", [])
        if candidates:
            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            return _coerce_text_content(parts)
        return ""
    if provider == "ollama":
        message = payload.get("message", {})
        if isinstance(message, dict):
            text = _coerce_text_content(message.get("content", ""))
            if text:
                return text
        return _coerce_text_content(payload.get("response", ""))

    if transport == "openai_responses":
        if isinstance(payload.get("output_text"), str):
            return payload["output_text"]
        parts = []
        for item in payload.get("output", []):
            if not isinstance(item, dict):
                continue
            for part in item.get("content", []):
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    text = part.get("output_text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    choices = payload.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        text = _coerce_text_content(message.get("content", ""))
        if text:
            return text
        if isinstance(choices[0].get("text"), str):
            return choices[0]["text"]
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    if isinstance(payload.get("text"), str):
        return payload["text"]
    return ""
