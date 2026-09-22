"""Detection and preservation rules for agent/tool protocol traffic."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def is_tool_protocol_message(message: Mapping[str, Any]) -> bool:
    """Return whether a message carries machine-readable tool state.

    Tool results and assistant tool-call envelopes are protocol data, not prose;
    lossily rewriting either can make a valid agent turn unparsable upstream.
    The key-presence check deliberately treats ``tool_calls: []`` as protocol
    state too, keeping malformed/partial envelopes fail-safe.
    """
    return message.get("role") == "tool" or "tool_calls" in message


def is_tool_result_compressible(message: Mapping[str, Any]) -> bool:
    """Return whether a message is a tool-result content carrier.

    The ``tool_call_id`` and the message envelope remain untouched; only the
    result's ``content`` is eligible for a lossless L1 transform. Assistant
    ``tool_calls`` (including malformed/empty values) are never eligible.
    """
    return message.get("role") == "tool" and "tool_calls" not in message


def is_tool_schema_compressible(tools_array: Any) -> bool:
    """Return whether ``tools`` is a non-empty array of JSON object schemas.

    Valid schemas are safe for whitespace-only serialization compaction. A
    partial or malformed array fails closed so it reaches the upstream exactly
    as supplied.
    """
    return (
        isinstance(tools_array, list)
        and bool(tools_array)
        and all(isinstance(tool, Mapping) for tool in tools_array)
    )


def has_tool_calling_state(body: Mapping[str, Any]) -> bool:
    """Return whether a request must bypass lossy/L1 body transforms.

    A declared non-empty ``tools`` list, any present ``tool_choice`` field, a
    message carrying ``tool_calls``, or a ``role=tool`` result is enough to
    activate the hard passthrough gate. This is intentionally conservative:
    protocol state must remain unchanged even when the envelope is partial.
    """
    if body.get("tools"):
        return True
    if "tool_choice" in body:
        return True
    messages = body.get("messages")
    if isinstance(messages, list):
        return any(
            isinstance(message, Mapping) and is_tool_protocol_message(message)
            for message in messages
        )
    return False
