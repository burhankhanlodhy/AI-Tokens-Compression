"""OpenAI-compatible adapter: covers OpenAI, OpenRouter, xAI, Google compat,
vLLM, Ollama — one class, registry rows differ by base_url + auth_style."""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import auth_headers_for, error_from_status
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


class OpenAICompatAdapter:
    """Wire shape: POST {base_url}/v1/chat/completions, Bearer auth by default.

    The adapter never sees compression logic; it translates NormalizedRequest
    <-> provider wire payloads only.
    """

    def __init__(self, name: str = "openai_compat", auth_style: str = "bearer",
                 chat_path: str = "/v1/chat/completions"):
        self.name = name
        self.auth_style = auth_style
        self.chat_path = chat_path

    # ---- routing ----

    def matches(self, model: str) -> bool:
        # OpenAI-compat is the default adapter for any model not claimed by a
        # more specific one; registry routing decides precedence.
        return True

    def normalize_model(self, model: str) -> str:
        # "openai/gpt-4o" -> "gpt-4o"; bare ids pass through.
        return model.split("/", 1)[1] if "/" in model else model

    # ---- auth (C2) ----

    def auth_headers(self, credential: str) -> dict[str, str]:
        return auth_headers_for(self.auth_style, credential)

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
                    parts.append({"type": "image_url",
                                  "image_url": {"url": src["url"]}})
                else:
                    parts.append({"type": "image_url",
                                  "image_url": {"url":
                                      f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}})
            else:  # "file" or unknown -> invalid_request at upstream; forward text
                parts.append({"type": "text", "text": p.text or ""})
        return parts

    def translate_request(self, req: NormalizedRequest) -> AdapterRequest:
        body: dict[str, Any] = {"model": self.normalize_model(req.model), "messages": []}
        if req.system:
            body["messages"].append({"role": "system", "content": req.system})
        for m in req.messages:
            entry: dict[str, Any] = {"role": m.role, "content": self._translate_content(m.content)}
            if m.tool_calls:
                entry["tool_calls"] = m.tool_calls
            if m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
            if m.name:
                entry["name"] = m.name
            body["messages"].append(entry)
        if req.tools:
            body["tools"] = req.tools
        if req.stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            body["temperature"] = req.temperature
        body.update(req.extra)  # provider passthrough (reasoning, etc.)
        return AdapterRequest(path=self.chat_path, headers={}, json_body=body)

    # ---- response translation (C4, C8, C9) ----

    def translate_response(self, raw: httpx.Response, req: NormalizedRequest) -> NormalizedResponse:
        content = raw.content
        if raw.status_code != 200:
            try:
                message = json.loads(content).get("error", {}).get("message", "") or content.decode(errors="replace")
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
            choices = data.get("choices") or []
            if choices:
                output_text = (choices[0].get("message") or {}).get("content") or ""
            u = data.get("usage") or {}
            if u:
                usage = Usage(
                    input_tokens=int(u.get("prompt_tokens", 0)),
                    output_tokens=int(u.get("completion_tokens", 0)),
                    cache_read_tokens=int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
                )
        except (json.JSONDecodeError, AttributeError, KeyError, ValueError):
            pass
        return NormalizedResponse(status=raw.status_code, content=content,
                                  output_text=output_text, usage=usage)

    # ---- streaming (C4, C5) ----

    def translate_stream_chunk(self, line: str, req: NormalizedRequest) -> StreamEvent:
        if not line.startswith("data: ") or line == "data: [DONE]":
            if line == "data: [DONE]":
                return StreamEvent(kind="done", raw_line=line)
            return StreamEvent(kind="delta", raw_line=line)  # comments/keepalives pass through

        try:
            data = json.loads(line[6:])
        except json.JSONDecodeError:
            return StreamEvent(kind="delta", raw_line=line)

        # usage-only chunk (Ollama/some versions omit deltas on it; OpenAI sends
        # it as final chunk with empty choices when stream_options.include_usage)
        u = data.get("usage")
        if u and not data.get("choices"):
            return StreamEvent(kind="usage", usage=Usage(
                input_tokens=int(u.get("prompt_tokens", 0)),
                output_tokens=int(u.get("completion_tokens", 0)),
                cache_read_tokens=int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            ), raw_line=line)

        choices = data.get("choices") or []
        delta = (choices[0].get("delta") or {}).get("content") if choices else None
        return StreamEvent(kind="delta", delta_text=delta or "", raw_line=line)
