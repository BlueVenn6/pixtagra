"""Provider metadata/usage boundaries use synthetic values only."""
from dataclasses import asdict

import pytest

from pixtagra_provider import ProviderRequest, ProviderResult, TokenUsage
from pixtagra_provider_legacy import LegacyProvider


@pytest.mark.parametrize("payload,expected", [
    ({}, None),
    ({"usage": None}, None),
    ({"usage": {"input_tokens": 0, "output_tokens": 0}}, TokenUsage(0, 0, None)),
    ({"usage": {"prompt_tokens": 21, "completion_tokens": 3, "total_tokens": 24}}, TokenUsage(21, 3, 24)),
    ({"usage": {"input_tokens": 21, "output_tokens": 3}}, TokenUsage(21, 3, None)),
    ({"usage": {"input_tokens": True, "output_tokens": -4, "total_tokens": "24"}}, None),
    ({"usage": {"input_tokens": 8, "provider_secret": "synthetic-secret"}}, TokenUsage(8, None, None)),
])
def test_usage_keeps_only_reported_nonnegative_integer_counts(payload, expected):
    assert TokenUsage.from_response(payload) == expected


def test_request_has_no_credential_and_does_not_print_images_or_prompts():
    request = ProviderRequest("Legacy", "openai", "custom-id", "https://provider.example/v1",
                              "openai_responses", "private-prompt", "private-image")
    assert "api_key" not in asdict(request)
    assert "private-prompt" not in repr(request)
    assert "private-image" not in repr(request)


def test_result_preserves_extension_metadata_without_logging_it():
    metadata = {"title": "private-title", "compliance": {}, "extension": ["unchanged"]}
    result = ProviderResult(metadata, "openai", "custom-id")
    assert result.metadata is metadata
    assert result.usage is None
    assert "private-title" not in repr(result)


def test_legacy_model_discovery_explicitly_unsupported_without_network():
    discovery = LegacyProvider(None).list_models()
    assert discovery.supported is False
    assert discovery.model_ids == ()
