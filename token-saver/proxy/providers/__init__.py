"""Provider adapter layer (PA-1). See adapter-api-surface.md."""
from .registry import DEFAULT_REGISTRY, ProviderRegistry, ProviderRow
from .model import (
    AdapterRequest,
    ContentPart,
    Message,
    NormalizedRequest,
    NormalizedResponse,
    ProviderError,
    StreamEvent,
    Usage,
)

__all__ = [
    "DEFAULT_REGISTRY",
    "ProviderRegistry",
    "ProviderRow",
    "AdapterRequest",
    "ContentPart",
    "Message",
    "NormalizedRequest",
    "NormalizedResponse",
    "ProviderError",
    "StreamEvent",
    "Usage",
]
