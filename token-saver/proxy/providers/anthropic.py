"""Anthropic adapter: /v1/messages wire shape (system-as-param, x-api-key,
anthropic-version header, distinct SSE event names). PA-4 hook: cache_control
blocks on system/content prefixes."""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import error_from_status
from .model import (
    AdapterRequest,
    ContentPart,
    NormalizedRequest,
    NormalizedResponse,
    ProviderError,
    StreamEvent,
    Usage,
)

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, messages_path: str = "/v1/messages"):
        self.messages_path = messages_path

    # ---- routing ----

    def matches(self, model: str) -> bool:
        return model.startswith("anthropic/") or model.startswith("claude-")

    def normalize_model(self, model: str) -> str:
        return model.split("/", 1)[1] if "/" in model else model

    # ---- auth (C2) ----

    def auth_headers(self, credential: str) -> dict[str, str]:
        return {"x-api-key": credential, "anthropic-version": ANTHROPIC_VERSION}

    # ---- request translation (C3) ----

    def _translate_content(self, content: str | list[ContentPart]) -> Any:
        if isinstance(content, str):
            return content
        parts: list[dict[str, Any]] = []
        for p in content:
            if p.type == "text":
                parts.append({"type": "text", "text": p.text or ""})
            elif p.type == "image":
                src = p.source or {}
                if src.get("url"):
                    # Anthropic requires base64 or a URL block (URL source supported
                    # on newer API versions); pass through as url source.
                    parts.append({"type": "image", "source": {"type": "url", "url": src["url"]}})
                else:
                    parts.append({"type": "image", "source": {
                        "type": "base64",
                        "media_type": src.get("media_type", "image/png"),
                        "data": src.get("data", ""),
                    }})
            else:
                parts.append({"type": "text", "text": p.text or ""})
        return parts

    @staticmethod
    def _anthropic_tool(tool: Any) -> Any:
        """One tool definition -> Anthropic {name, description, input_schema}.

        Idempotent (AC-A4): already-Anthropic tools pass through unchanged;
        OpenAI-shaped tools — wrapped ({type: function, function: {...}}) or
        bare — get the required parameters->input_schema rename, and
        OpenAI-only keys (strict) are dropped because Anthropic rejects them.
        """
        if not isinstance(tool, dict):
            return tool
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if not isinstance(fn, dict) or "input_schema" in fn:
            return fn if isinstance(fn, dict) else tool
        translated: dict[str, Any] = {"name": fn.get("name", "")}
        if fn.get("description") is not None:
            translated["description"] = fn["description"]
        translated["input_schema"] = fn.get("parameters") or {
            "type": "object", "properties": {},
        }
        return translated

    def translate_request(self, req: NormalizedRequest) -> AdapterRequest:
        body: dict[str, Any] = {
            "model": self.normalize_model(req.model),
            "messages": [],
            "max_tokens": req.max_tokens if req.max_tokens is not None else 4096,
        }
        if req.system:
            # PA-4 hook: static system prefixes become cache_control blocks.
            body["system"] = req.system
        for m in req.messages:
            if m.role == "system":
                continue  # extracted to body["system"] upstream of this loop
            entry: dict[str, Any] = {"role": m.role, "content": self._translate_content(m.content)}
            if m.tool_calls:
                entry["content"] = [
                    {"type": "tool_use", "id": tc.get("id", ""),
                     "name": tc.get("function", {}).get("name", ""),
                     "input": tc.get("function", {}).get("arguments", {})}
                    for tc in m.tool_calls
                ]
            if m.tool_call_id:
                entry = {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": m.tool_call_id,
                     "content": m.content if isinstance(m.content, str) else ""}
                ]}
            body["messages"].append(entry)
        if req.tools:
            body["tools"] = [self._anthropic_tool(t) for t in req.tools]
        if req.stream:
            body["stream"] = True
        # `reasoning` is OpenAI/OpenRouter-shaped; Anthropic has no such param.
        extra = {k: v for k, v in req.extra.items() if k != "reasoning"}
        body.update(extra)
        return AdapterRequest(path=self.messages_path, headers={}, json_body=body)

    # ---- response translation (C4, C8, C9) ----

    def translate_response(self, raw: httpx.Response, req: NormalizedRequest) -> NormalizedResponse:
        content = raw.content
        if raw.status_code != 200:
            try:
                data = json.loads(content)
                message = data.get("error", {}).get("message", "") or content.decode(errors="replace")
            except Exception:
                message = content.decode(errors="replace")
            retry_after = raw.headers.get("retry-after")
            return NormalizedResponse(
                status=raw.status_code, content=content,
                error=error_from_status(raw.status_code, message,
                                        float(retry_after) if retry_after else None),
            )
        output_text = ""
        usage: Usage | None = None
        try:
            data = json.loads(content)
            blocks = data.get("content") or []
            output_text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            u = data.get("usage") or {}
            if u:
                usage = Usage(
                    input_tokens=int(u.get("input_tokens", 0)),
                    output_tokens=int(u.get("output_tokens", 0)),
                    cache_read_tokens=int(u.get("cache_read_input_tokens", 0)),
                    cache_write_tokens=int(u.get("cache_creation_input_tokens", 0)),
                )
        except (json.JSONDecodeError, AttributeError, KeyError, ValueError):
            pass
        return NormalizedResponse(status=raw.status_code, content=content,
                                  output_text=output_text, usage=usage)

    # ---- streaming (C4) ----

    def translate_stream_chunk(self, line: str, req: NormalizedRequest) -> StreamEvent:
        """Anthropic SSE line -> StreamEvent.

        In translated mode the caller re-emits ONLY OpenAI chunks, so
        non-translatable lines (event:, ping, malformed) return raw_line=""
        meaning "drop" — the caller decides whether to pass through.
        """
        # Anthropic SSE uses "event: X" lines followed by the "data:" line;
        # translate only on the data line (event name is embedded in the payload).
        if line.startswith("event: "):
            return StreamEvent(kind="drop", raw_line="")
        if not line.startswith("data: "):
            if line.strip() == "":
                return StreamEvent(kind="drop", raw_line="")
            return StreamEvent(kind="drop", raw_line="")  # comments/pings dropped
        payload = line[6:]
        if payload == "[DONE]":
            return StreamEvent(kind="done", raw_line=line)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # malformed data line: surface as a drop-able event, never a crash
            return StreamEvent(kind="malformed", raw_line=line)

        etype = data.get("type")
        if etype == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                # tool_use header: id + name travel in the event payload
                return StreamEvent(kind="tool_start",
                                   delta_text=block.get("id", ""),
                                   raw_line=block.get("name", ""))
            return StreamEvent(kind="drop", raw_line="")
        if etype == "content_block_delta":
            d = data.get("delta") or {}
            if d.get("type") == "input_json_delta":
                # tool-call argument deltas: partial JSON string
                return StreamEvent(kind="tool_delta",
                                   delta_text=d.get("partial_json", ""),
                                   raw_line=line)
            return StreamEvent(kind="delta", delta_text=d.get("text", ""), raw_line=line)
        if etype == "message_start":
            u = (data.get("message") or {}).get("usage") or {}
            return StreamEvent(kind="usage", usage=Usage(
                input_tokens=int(u.get("input_tokens", 0)),
                output_tokens=int(u.get("output_tokens", 0)),
                cache_read_tokens=int(u.get("cache_read_input_tokens", 0)),
                cache_write_tokens=int(u.get("cache_creation_input_tokens", 0)),
            ), raw_line=line)
        if etype == "message_delta":
            u = data.get("usage") or {}
            if u:
                return StreamEvent(kind="usage", usage=Usage(
                    output_tokens=int(u.get("output_tokens", 0)),
                ), raw_line=line)
        if etype == "message_stop":
            return StreamEvent(kind="done", raw_line=line)
        if etype == "error":
            e = data.get("error") or {}
            return StreamEvent(kind="error", error=ProviderError(
                kind=e.get("type", "upstream"), message=e.get("message", ""),
                retry_after_s=None, status=500,
            ), raw_line=line)
        # ping / content_block_start / content_block_stop / others
        return StreamEvent(kind="drop", raw_line="")
