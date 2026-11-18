"""Provider routing settings must never reuse another provider's endpoint."""
import pixtagra_models as models


def test_model_lifecycle_policy_has_one_recommended_live_checked_default():
    recommended = [
        name for name, policy in models.MODEL_LIFECYCLE.items()
        if policy["recommended"]
    ]
    assert recommended == [models.RECOMMENDED_MODEL]
    assert models.RECOMMENDED_MODEL == "OpenAI GPT-5.6 Terra"
    assert models.PROVIDERS[models.RECOMMENDED_MODEL]["requires_live_check"] is True
    assert {name for name in models.SELECTABLE_MODELS if name.startswith("OpenAI GPT-")} == {
        "OpenAI GPT-5.6 Sol", "OpenAI GPT-5.6 Terra", "OpenAI GPT-5.6 Luna",
    }


def test_legacy_relay_belongs_only_to_the_saved_model_provider():
    config = {"model": "OpenAI GPT-5.5", "base_url": "https://relay.example/v1"}
    urls = models.configured_base_urls(config)
    assert urls == {"openai": "https://relay.example/v1"}
    for provider in ("kimi", "qwen", "minimax"):
        assert urls.get(provider, "") == ""
    assert "provider_base_urls" not in config


def test_switching_providers_and_restarting_restores_each_endpoint():
    config = {"model": "OpenAI GPT-5.5", "base_url": "https://relay.example/v1"}
    config = models.remember_provider_base_url(config, "Moonshot Kimi", "https://kimi-relay.example/v1")
    config.update(model="Moonshot Kimi", base_url="https://kimi-relay.example/v1")
    assert models.configured_base_urls(config) == {
        "openai": "https://relay.example/v1", "kimi": "https://kimi-relay.example/v1"
    }
    config = models.remember_provider_base_url(config, "OpenAI GPT-5.5", "")
    assert models.configured_base_urls(config)["openai"] == ""
    assert models.configured_base_urls(config)["kimi"] == "https://kimi-relay.example/v1"


def test_local_switch_and_malformed_settings_do_not_replace_cloud_endpoints():
    config = {"model": "OpenAI GPT-5.5", "base_url": "https://relay.example"}
    updated = models.remember_provider_base_url(config, "Local Ollama", "http://localhost:11434")
    assert models.configured_base_urls(updated) == {"openai": "https://relay.example"}
    assert models.configured_base_urls({"provider_base_urls": [], "base_url": 42}) == {}


def test_provider_metadata_is_consistent_with_request_catalog():
    for name, config in models.API_CONFIG.items():
        provider = models.get_provider_config(name)
        assert provider["provider"] == config["provider"]
        assert provider["stability"] == config["stability"]
        assert provider["fallback_eligible"] == config["fallback_eligible"]
        assert provider["requires_api_key"] == (config["provider"] != "ollama")


def test_removed_gpt_selection_retains_openai_relay_without_sharing_it():
    config = {"model": "OpenAI GPT-4.1", "base_url": "https://relay.example/v1"}
    assert models.migrate_model_selection(config["model"]) == "OpenAI GPT-5.5"
    assert models.configured_base_urls(config) == {"openai": "https://relay.example/v1"}
    assert models._legacy_config_provider_id(config["model"]) == "openai"
    assert "OpenAI GPT-4.1" not in models.API_CONFIG


def test_gpt_56_candidates_use_existing_openai_responses_route_and_are_not_fallbacks():
    assert models.OPENAI_GPT_56_CANDIDATES == (
        ("OpenAI GPT-5.6 Sol", "gpt-5.6-sol"),
        ("OpenAI GPT-5.6 Terra", "gpt-5.6-terra"),
        ("OpenAI GPT-5.6 Luna", "gpt-5.6-luna"),
    )
    for display_name, model_id in models.OPENAI_GPT_56_CANDIDATES:
        config = models.API_CONFIG[display_name]
        assert config["model"] == model_id
        assert config["provider"] == "openai"
        assert config["protocol"] == "responses"
        assert config["fallback_eligible"] is False
        assert config["requires_live_check"] is True
        assert models.migrate_model_selection(display_name) == display_name
