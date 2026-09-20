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


def has_tool_calling_state(body: Mapping[str, Any]) -> bool:
    """Return whether a request must bypass all body transforms.

    A declared non-empty ``tools`` list, any present ``tool_choice`` field, a
    message carrying ``tool_calls``, or a ``role=tool`` result is enough to
    activate the hard passthrough gate. This is intentionally conservative:
    protocol state must remain byte-identical even when the envelope is partial.
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
