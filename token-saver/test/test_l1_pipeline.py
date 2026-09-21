"""B2 integration: L1 pipeline ordering through the live proxy route.

With L1_ENABLED=true:
- upstream receives the CLEANED messages (whitespace-compacted JSON)
- an identical re-request produces the same clean bytes (cache-key stability)
- the ledger row records l1_tokens_stripped > 0 for a strippable prompt
- L1_ENABLED=false (explicitly pinned; ON is the default since B-26) sends
  the upstream body BYTE-IDENTICAL — the full messages list, not merely an
  un-cleaned content string
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy.config import get_settings

RAG = json.dumps({"content": "TTL default is 3600 seconds.",
                  "score": 0.97, "retrieved_at": "2026-09-14"}, indent=2)

UPSTREAM_RESPONSE = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


@pytest.fixture
def l1_env(tmp_path, monkeypatch):
    # The L1 pipeline assertions use the local SQLite ledger.
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _app(transport_handler):
    from proxy.main import app
    app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler),
        base_url="http://upstream.test/v1")
    return app


@pytest_asyncio.fixture
async def capturing(l1_env, monkeypatch):
    monkeypatch.setenv("L1_ENABLED", "true")
    get_settings.cache_clear()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    app = _app(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, captured
    await app.state.http.aclose()


@pytest.mark.asyncio
async def test_upstream_receives_clean_messages(capturing):
    c, captured = capturing
    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": RAG}]},
    )
    assert resp.status_code == 200
    sent = captured["body"]["messages"][0]["content"]
    # v1.1: retrieved_at is negative-list (timestamps conserved)
    assert sent == json.dumps(
        {"content": "TTL default is 3600 seconds.", "retrieved_at": "2026-09-14"},
        separators=(",", ":"))
    # dead fields gone, reserved content intact
    obj = json.loads(sent)
    assert "score" not in obj
    assert obj["content"] == "TTL default is 3600 seconds."


@pytest.mark.asyncio
async def test_ledger_records_l1_tokens_and_decomposes_cost(capturing, monkeypatch):
    """AC-P1f: a non-cache L1 row is a cost_saved portion, never an addend.

    The fixture's JSON/RAG request is classified passthrough, exercising the
    v1.2 independent L1 gate and the local fallback ledger that developers
    actually run without a Postgres DSN.
    """
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    from proxy import stats
    prompt = _pinned_production_case("rag-001", "rag")
    assert prompt["messages"] != [{"role": "user", "content": RAG}]
    from proxy.classifier import classify
    assert classify(prompt["messages"]) == "passthrough"
    stats.init_db()  # test_proxy's tmp_db fixture does this; standalone here
    c, captured = capturing
    await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "gpt-4o", "messages": prompt["messages"]},
    )
    data = stats.aggregate_stats()
    assert data["totals"]["input_tokens_saved"] > 0
    # B2-d: assert on the L1 column ITSELF — input_tokens_saved is satisfied
    # by the compression path alone and can never catch a dropped l1_* value.
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT route, input_tokens_before, input_tokens_after, "
            "est_cost_before, est_cost_after, l1_tokens_stripped, l1_savings "
            "FROM requests "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "no ledger row persisted"
    assert row["route"] == "passthrough"
    assert row["l1_tokens_stripped"] > 0
    assert row["l1_savings"] > 0
    assert row["l1_tokens_stripped"] <= (
        row["input_tokens_before"] - row["input_tokens_after"]
    )
    assert 0 < row["l1_savings"] <= (
        row["est_cost_before"] - row["est_cost_after"] + 1e-12
    )


@pytest.mark.asyncio
async def test_l1_disabled_leaves_upstream_byte_identical(l1_env, monkeypatch):
    # B-26: the default flipped to ON, so the off-case must pin the env
    # explicitly — it is no longer the default the fixture inherits.
    monkeypatch.setenv("L1_ENABLED", "false")
    get_settings.cache_clear()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    app = _app(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key-123"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": RAG}]},
        )
    await app.state.http.aclose()
    assert resp.status_code == 200
    # B-26 acceptance: BYTE-IDENTICAL upstream — the FULL messages list,
    # not merely "content was not cleaned".
    assert captured["body"]["messages"] == [
        {"role": "user", "content": RAG}
    ]


@pytest.mark.asyncio
async def test_l1_on_by_default_cleans_upstream(l1_env):
    # B-26 companion: with NO env pin at all (l1_env only clears the settings
    # cache), the flipped default means the strippable prompt reaches the
    # upstream already cleaned — exactly what clean_messages() produces.
    from proxy.l1_clean import clean_messages

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    app = _app(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key-123"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": RAG}]},
        )
    await app.state.http.aclose()
    assert resp.status_code == 200
    sent = captured["body"]["messages"][0]["content"]
    raw = [{"role": "user", "content": RAG}]
    assert sent != RAG  # default-on actually transformed the strippable body
    assert captured["body"]["messages"] == [
        {"role": "user", "content": clean_messages(raw)[0]["content"]}
    ]


# ---------------------------------------------------------------- B2 P0 (QA):
# a passthrough-classified request must reach upstream BYTE-IDENTICAL,
# independent of L1_ENABLED / COMPRESSION_ENABLED (taxonomy v1.1 §5, line 24:
# "L1 is off for passthrough"). Production path: classify runs on RAW bytes
# pre-L1, and L1 is skipped for passthrough routes.

CODE = '''```python
def ttl():
    return 3600
```
'''


def _passthrough_capture(monkeypatch, tmp_path, *, l1, comp):
    """App + capture dict with L1_ENABLED/COMPRESSION_ENABLED pinned.

    MIN_CHARS_TO_CLASSIFY is lowered so the short RAG fixture genuinely
    classifies as passthrough (default 120 would route it to compress)."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    monkeypatch.setenv("L1_ENABLED", "true" if l1 else "false")
    monkeypatch.setenv("COMPRESSION_ENABLED", "true" if comp else "false")
    monkeypatch.setenv("MIN_CHARS_TO_CLASSIFY", "10")
    get_settings.cache_clear()
    from proxy import stats
    stats.init_db()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    from proxy.main import app
    app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://upstream.test/v1")
    return app, captured


def _post(app, content):
    import asyncio

    async def _run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            return await c.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key-123"},
                json={"model": "gpt-4o-mini",
                      "messages": [{"role": "user", "content": content}]},
            )

    try:
        return asyncio.run(_run())
    finally:
        asyncio.run(app.state.http.aclose())


@pytest.mark.parametrize(
    ("l1", "comp", "content"),
    [
        (True, True, CODE),
        (True, False, CODE),
        (False, True, CODE),
        (False, False, CODE),
        (False, True, RAG),
        (False, False, RAG),
    ],
    ids=[
        "code-True-True", "code-True-False", "code-False-True",
        "code-False-False", "json_rag-False-True", "json_rag-False-False",
    ],
)
def test_passthrough_reaches_upstream_byte_identical(
        tmp_path, monkeypatch, l1, comp, content):
    """Lossy and CODE passthrough remains byte-identical at the wire boundary.

    JSON/RAG with L1 enabled is intentionally covered by the AC-P1f
    round-trip regression below: its contract is reversible cleaning rather
    than raw-byte identity.
    """
    app, captured = _passthrough_capture(monkeypatch, tmp_path,
                                         l1=l1, comp=comp)
    resp = _post(app, content)
    assert resp.status_code == 200
    assert captured["body"]["messages"][0]["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize("comp", [True, False], ids=["compression-on", "compression-off"])
async def test_l1_json_rag_round_trip_and_clean_cache_key(
        l1_env, monkeypatch, comp):
    """AC-P1f: the live route reproducibly cleans JSON/RAG before caching.

    This intentionally drives the ASGI handler, not clean_messages() alone:
    both requests must produce identical cleaned upstream bytes, and the
    cache seam must receive the cleaned body whose independently computed key
    differs from the raw-body key when the static prefix is cleaned.
    """
    monkeypatch.setenv("L1_ENABLED", "true")
    monkeypatch.setenv("COMPRESSION_ENABLED", "true" if comp else "false")
    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    monkeypatch.setenv("CACHE_ENABLED", "true")
    get_settings.cache_clear()

    from proxy import caching, main, stats
    from proxy.l1_clean import clean_messages

    raw_body = {
        "model": "openai/gpt-4o",
        "messages": [
            {"role": "system", "content": RAG},
            {"role": "user", "content": "What is the TTL?"},
        ],
    }
    expected_messages = clean_messages(raw_body["messages"])
    expected_body = {**raw_body, "messages": expected_messages}
    assert expected_messages != raw_body["messages"]

    cache_calls: list[tuple[str, str, str, dict]] = []
    upstream_messages: list[list[dict]] = []

    def fake_lookup(provider, model, body):
        cache_calls.append(("lookup", provider, model, body))
        return None

    def fake_record(provider, model, body):
        cache_calls.append(("record", provider, model, body))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        upstream_messages.append(body["messages"])
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    monkeypatch.setattr(caching, "lookup", fake_lookup)
    monkeypatch.setattr(caching, "record", fake_record)
    stats.init_db()

    provider_base = get_settings().provider_base_urls["openai"]
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=provider_base
    )
    main.app.state.http = None
    main.app.state.http_clients = {provider_base: upstream}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            for _ in range(2):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer test-key-123"},
                    json=raw_body,
                )
                assert response.status_code == 200
    finally:
        await upstream.aclose()
        main.app.state.http_clients = {}
        get_settings.cache_clear()

    assert upstream_messages == [expected_messages, expected_messages]
    assert len(cache_calls) == 4
    assert all(call[3] == expected_body for call in cache_calls)

    clean_keys = {
        caching.cache_key(caching.canonical_prefix(call[3]), call[2], call[1])
        for call in cache_calls
    }
    raw_key = caching.cache_key(
        caching.canonical_prefix(raw_body), raw_body["model"], "openai"
    )
    assert len(clean_keys) == 1
    assert next(iter(clean_keys)) != raw_key


# ---------------------------------------------------------------- B2 P1 (QA):
# L1 is lossless and therefore has its own eligibility gate. It must run on
# the real production path even when the raw request is classified passthrough;
# the route gate remains responsible for lossy compression only. These are
# deliberately pinned corpus rows, not hand-written helper fixtures, so a
# route-gating regression makes the production yield visibly return to zero.
PINNED_PRODUCTION_CASES = (
    ("rag-001", "rag"),
    ("json_doc-001", "json_doc"),
    ("system_dup-001", "system_dup"),
)


def _pinned_production_case(fixture_id: str, category: str) -> dict:
    import hashlib

    fixtures = Path(__file__).resolve().parent.parent / "benchmark" / "fixtures"
    fixture_file = fixtures / "l1_prompts.json"
    checksum_file = fixtures / "l1_prompts.json.sha256"
    expected = checksum_file.read_text().split()[0].strip()
    actual = hashlib.sha256(fixture_file.read_bytes()).hexdigest()
    assert actual == expected, "pinned L1 fixture checksum mismatch"
    prompts = json.loads(fixture_file.read_text())["prompts"]
    matches = [p for p in prompts if p["id"] == fixture_id]
    assert len(matches) == 1, f"expected exactly one pinned fixture: {fixture_id}"
    prompt = matches[0]
    assert prompt["category"] == category
    return prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_id", "category"),
    PINNED_PRODUCTION_CASES,
    ids=[fixture_id for fixture_id, _ in PINNED_PRODUCTION_CASES],
)
async def test_l1_reduces_pinned_corpus_on_real_production_path(
        capturing, fixture_id, category):
    """AC-P1e/f guard: each representative passthrough corpus row is reduced.

    The request is sent through the ASGI handler and checked at the captured
    upstream boundary. This must not be replaced by a direct clean_messages()
    assertion: that was the false-green gap which hid zero customer-facing
    yield on the pinned corpus.
    """
    from proxy.classifier import classify
    from proxy.counting import count_messages
    from proxy.l1_clean import clean_messages

    prompt = _pinned_production_case(fixture_id, category)
    messages = prompt["messages"]
    assert classify(messages) == "passthrough", fixture_id
    before = count_messages(messages, "gpt-4o")
    expected_messages = clean_messages(messages)
    expected_after = count_messages(expected_messages, "gpt-4o")
    assert expected_after < before, f"fixture has no L1 reduction: {fixture_id}"

    from proxy import stats
    stats.init_db()
    c, captured = capturing
    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "gpt-4o", "messages": messages},
    )
    assert resp.status_code == 200
    sent_messages = captured["body"]["messages"]

    # Wire-level production assertion: upstream receives the L1-cleaned
    # payload, and its real tokenizer count is lower than the raw request.
    assert sent_messages == expected_messages, fixture_id
    assert count_messages(sent_messages, "gpt-4o") < before, fixture_id
    assert count_messages(sent_messages, "gpt-4o") == expected_after, fixture_id
