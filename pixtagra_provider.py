"""Provider contracts independent of Tk, credentials and workspace persistence.

Keys are supplied only to the operation, never stored in requests/results.
The legacy dict remains the metadata representation during gradual migration.
"""
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderRequest:
    model_name: str
    provider: str
    model_id: str
    endpoint: str
    transport: str
    prompt: str = field(repr=False)
    image_base64: str = field(repr=False)
    max_tokens: int = 2048
    response_kind: str = "metadata"
    timeout: tuple[int, int] = (15, 180)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_response(cls, payload):
        """Accept only counts actually reported; do not infer or price usage."""
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return None

        def count(*names):
            for name in names:
                value = usage.get(name)
                if type(value) is int and value >= 0:
                    return value
            return None

        result = cls(count("input_tokens", "prompt_tokens"),
                     count("output_tokens", "completion_tokens"), count("total_tokens"))
        return result if any(value is not None for value in (
            result.input_tokens, result.output_tokens, result.total_tokens
        )) else None


@dataclass(frozen=True)
class ProviderResult:
    # Preserve the exact legacy metadata object, including extension fields.
    metadata: dict[str, Any] | None = field(repr=False)
    provider: str
    model_id: str
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class ModelDiscovery:
    supported: bool = False
    model_ids: tuple[str, ...] = ()


class Provider(Protocol):
    def generate(self, request: ProviderRequest, *, api_key: str) -> ProviderResult: ...

    def list_models(self) -> ModelDiscovery: ...
