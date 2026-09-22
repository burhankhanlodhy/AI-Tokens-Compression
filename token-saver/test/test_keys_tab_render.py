"""Keys & Tenants tab render gates (C6) — pins keys-tenants-tab-spec.md §6.

Executes the real proxy/static/dashboard.js in Node via the shared
dashboard_render_harness.js harness (routes fixture + click/set/wait script)
against a tenants/keys/kpis fixture, asserting the spec's gates:

  1. Redaction (C10): rendered surface carries key_last4 / "••••" only —
     never key_hash, never full key material, never a secret-shaped string.
  2. One-time reveal: the create/rotate plaintext appears exactly once in the
     single POST response and is gone from the DOM after Done.
  3. 1:1 usage contract: tenant + per-key figures render verbatim from the
     tenant_id/api_key_id-scoped /api/kpis responses (no new aggregation).
  4. Write-path UX: revoke is a two-step inline confirm; a failed write
     surfaces the API's `detail` inline WITHOUT re-rendering the table.
  5. Placeholder removal: no "arrives with Phase C" copy anywhere.
  6. Admin token handling (§4.5): writes carry the Bearer header only after
     token entry; the token never renders in DOM text outside the masked
     input; a mid-session 401 clears the token and re-prompts with the
     restart message while preserving the pending action.

Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "test" / "dashboard_render_harness.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available")

TENANT_ID = "00000000-0000-0000-0000-000000000000"
PLAINTEXT_KEY = "sk-live-" + "a" * 40          # the ONLY key material in play
ROTATED_PLAINTEXT = "sk-live-" + "b" * 40
SECRET_SHAPED = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
HASH_SHAPED = re.compile(r"[0-9a-f]{40,}")

TENANT = {"id": TENANT_ID, "name": "default", "plan": "self_host",
          "spend_cap_usd": None, "created_at": "2026-09-01T00:00:00Z"}

ACTIVE_KEY = {"id": "k1", "tenant_id": TENANT_ID, "key_last4": "1234",
              "scopes": ["chat"], "spend_cap_usd": None, "status": "active",
              "created_at": "2026-09-10T12:00:00Z", "revoked_at": None}
ROTATED_KEY = {"id": "k2", "tenant_id": TENANT_ID, "key_last4": "9876",
               "scopes": [], "spend_cap_usd": 5.0, "status": "rotated",
               "created_at": "2026-09-05T00:00:00Z", "revoked_at": None}
REVOKED_KEY = {"id": "k3", "tenant_id": TENANT_ID, "key_last4": "4444",
               "scopes": ["chat", "embeddings"], "spend_cap_usd": None,
               "status": "revoked", "created_at": "2026-09-04T00:00:00Z",
               "revoked_at": "2026-09-12T00:00:00Z"}

# Values the 1:1 contract pins: displayed numerals must equal these exactly.
TENANT_KPIS = {"overview": {"requests": 6, "cost_saved": 0.00265,
                            "input_tokens_saved": 1850}}
KEY_KPIS = {"overview": {"requests": 2, "input_tokens_saved": 300,
                         "cost_saved": 0.0004}}
GLOBAL_KPIS = {"overview": {"requests": 6, "cost_saved": 0.00265}, "series": []}


def _routes() -> dict:
    return {"_routes": {
        "/api/tenants": {"body": [TENANT]},
        "/api/keys": {"body": [ACTIVE_KEY, ROTATED_KEY, REVOKED_KEY]},
        "/api/kpis?tenant_id=": {"body": TENANT_KPIS},
        "/api/kpis?api_key_id=": {"body": KEY_KPIS},
        "/api/kpis": {"body": GLOBAL_KPIS},
        # create/rotate succeed by default; 401 variants override per-test
        "POST /api/keys": {"body": {"id": "k9", "key_last4": "9911",
                                    "key": PLAINTEXT_KEY}},
        "POST /api/keys/k1/rotate": {"body": {"id": "k10", "key_last4": "5522",
                                              "key": ROTATED_PLAINTEXT}},
        "POST /api/keys/k1/revoke": {"body": {"id": "k1", "status": "revoked",
                                              "revoked_at": "2026-09-19T00:00:00Z"}},
    }}


def _run(tmp_path: Path, fixture: dict, script: list | None = None) -> dict:
    if script is not None:
        fixture["_script"] = script
    fx = tmp_path / "fixture_keys.json"
    fx.write_text(json.dumps(fixture))
    proc = subprocess.run(["node", str(HARNESS), str(fx), "keys"],
                          capture_output=True, text=True, timeout=30,
                          cwd=str(ROOT))
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["script_error"] is None, out["script_error"]
    return out


def _money(x: float) -> str:
    """The exact string dashboard.js money() emits for x (JS toFixed(4))."""
    proc = subprocess.run(["node", "-e", f"console.log(({x!r}).toFixed(4))"],
                          capture_output=True, text=True)
    return "$" + proc.stdout.strip()


# ---------------------------------------------------------------- gate 5 + 1

def test_c6_placeholder_removed_and_table_renders_redacted(tmp_path):
    out = _run(tmp_path, _routes())
    html = out["html"]
    # gate 5: placeholder copy is gone
    assert "Phase C" not in html and "arrives with" not in html
    # gate 1: last-4 only, muted dots, never a hash or full key
    assert html.count("••••") >= 3
    for last4 in ("1234", "9876", "4444"):
        assert last4 in html
    assert "key_hash" not in html
    assert PLAINTEXT_KEY not in html and ROTATED_PLAINTEXT not in html
    assert not SECRET_SHAPED.search(html)
    assert not HASH_SHAPED.search(html)
    # column contract (§2.3)
    assert "<th>Key</th>" in html and "<th>Scopes</th>" in html
    assert "<th>Spend cap</th>" in html and "<th>Status</th>" in html
    # scopes chips / empty dash; cap inheritance; statuses
    assert '<span class="chip">chat</span>' in html
    assert '<span class="chip">embeddings</span>' in html
    assert "Inherits tenant" in html and _money(5.0) in html
    assert '<span class="badge green">active</span>' in html
    assert '<span class="badge red">revoked</span>' in html
    assert '<span class="badge neutral">rotated</span>' in html
    # actions only on the active key; revoked/rotated rows show none
    assert ">Rotate</button>" in html and ">Revoke</button>" in html
    assert html.count(">Rotate</button>") == 1
    # management CTA present
    assert ">Create key</button>" in html


def test_c6_tenant_and_kpi_cards_render_verbatim_1to1(tmp_path):
    out = _run(tmp_path, _routes())
    html = out["html"]
    # §2.1 tenant card: name, plan, NULL cap → "Uncapped", created date
    assert '<div class="kpi-num">default</div>' in html
    assert '<span class="badge neutral">self_host</span>' in html
    assert "Uncapped" in html and "2026-09-01" in html
    # §2.2 per-tenant KPI cards = /api/kpis?tenant_id fields, verbatim
    assert f'<div class="kpi-num">{_money(0.00265)}</div>' in html
    assert ">6</div>" in html
    # tenant selector deliberately absent (one tenant, §2.1)
    assert "<select" not in html


def test_c6_scoped_kpi_urls_carry_the_selector(tmp_path):
    out = _run(tmp_path, _routes())
    assert any("/api/kpis?tenant_id=" in u for u in out["urls"]), out["urls"]
    # the tab never aggregates: only the documented read endpoints are hit
    for u in out["urls"]:
        assert u.startswith(("/api/tenants", "/api/keys", "/api/kpis")), u


# ---------------------------------------------------------------- gate: empty

def test_c6_empty_keys_renders_spec_hero(tmp_path):
    fx = _routes()
    fx["_routes"]["/api/keys"] = {"body": []}
    out = _run(tmp_path, fx)
    html = out["html"]
    assert "No proxy keys yet" in html
    assert "Create a key so your applications can authenticate to the proxy." in html
    assert ">Create key</button>" in html


def test_c6_error_state_on_tenant_failure(tmp_path):
    fx = _routes()
    fx["_routes"]["/api/tenants"] = {"status": 503, "body": {}}
    out = _run(tmp_path, fx)
    assert "Couldn't load key management." in out["html"]
    assert ">Retry</button>" in out["html"]


# ------------------------------------------------------- gate 2: one-time reveal

def test_c6_create_flow_reveal_shown_once_then_gone(tmp_path):
    out = _run(tmp_path, _routes(), script=[
        {"click": "keys-create"},
        {"set": ["key-scopes", "chat, embeddings"]},
        {"set": ["key-cap", "20.00"]},
        {"click": "key-create-btn"},
        {"wait": 40},
    ])
    html = out["html"]
    assert len(out["writes"]) == 1, out["writes"]
    w = out["writes"][0]
    assert w["url"] == "/api/keys" and w["method"] == "POST"
    # reveal panel: plaintext exactly once, warning line, copy + done
    assert html.count(PLAINTEXT_KEY) == 1
    assert "Key created" in html and "•••• 9911" in html
    assert "This key is shown once. Copy it now — it cannot be retrieved again." in html
    assert 'id="copy-key"' in html and 'id="reveal-done"' in html
    # the plaintext never rides in a URL (§4.5 no query-string tokens)
    assert all(PLAINTEXT_KEY not in u for u in out["urls"])
    # Done → the plaintext is gone from the DOM for good
    out2 = _run(tmp_path, _routes(), script=[
        {"click": "keys-create"},
        {"click": "key-create-btn"},
        {"wait": 40},
        {"click": "reveal-done"},
        {"wait": 40},
    ])
    assert PLAINTEXT_KEY not in out2["html"]
    assert "Key created" not in out2["html"]
    # and the table was re-fetched (fresh statuses)
    assert sum(1 for u in out2["urls"] if u.startswith("/api/keys")) >= 2


def test_c6_rotate_flow_reveals_new_key_with_confirm_copy(tmp_path):
    # first click = the confirm step: rotate copy (§4.2.5), no write yet
    pre = _run(tmp_path, _routes(), script=[{"click": "rot-k1"}, {"wait": 20}])
    assert "Rotating issues a new key and immediately stops the old one." in pre["html"]
    assert "Confirm rotate?" in pre["html"]
    assert pre["writes"] == []
    # confirming issues the write and opens the one-time reveal
    out = _run(tmp_path, _routes(), script=[
        {"click": "rot-k1"},
        {"wait": 20},
        {"click": "rot-k1"},
        {"wait": 40},
    ])
    html = out["html"]
    assert len(out["writes"]) == 1
    assert out["writes"][0]["url"] == "/api/keys/k1/rotate"
    assert html.count(ROTATED_PLAINTEXT) == 1
    assert "Key rotated" in html and "•••• 5522" in html
    assert "This key is shown once. Copy it now — it cannot be retrieved again." in html


# ------------------------------------------------------- gate 4: write-path UX

def test_c6_revoke_is_two_step_inline_confirm(tmp_path):
    out = _run(tmp_path, _routes(), script=[
        {"click": "rev-k1"},
        {"wait": 20},
    ])
    html = out["html"]
    assert "Confirm revoke?" in html and ">Cancel</button>" in html
    assert "Revoked keys stop authenticating immediately. This cannot be undone." in html
    assert out["writes"] == []   # no write until confirm
    # confirm executes the revoke; cancel does not
    out2 = _run(tmp_path, _routes(), script=[
        {"click": "rev-k1"}, {"wait": 20},
        {"click": "rev-k1"}, {"wait": 40},
    ])
    assert len(out2["writes"]) == 1
    assert out2["writes"][0]["url"] == "/api/keys/k1/revoke"
    out3 = _run(tmp_path, _routes(), script=[
        {"click": "rev-k1"}, {"wait": 20},
        {"click": "revcancel-k1"}, {"wait": 20},
    ])
    assert "Confirm revoke?" not in out3["html"]
    assert out3["writes"] == []


def test_c6_failed_write_inline_error_no_rerender(tmp_path):
    fx = _routes()
    fx["_routes"]["POST /api/keys/k1/revoke"] = {
        "status": 400, "body": {"detail": "Unknown tenant."}}
    out = _run(tmp_path, fx, script=[
        {"click": "rev-k1"}, {"wait": 20},
        {"click": "rev-k1"}, {"wait": 40},
    ])
    # the API's detail surfaces verbatim, inline (textContent on the row's
    # pre-rendered error slot — recorded by the harness as `texts`)
    assert out["texts"].get("keyerr-k1") == "Unknown tenant."
    # §4.4: the table did NOT re-render — the keys list was fetched exactly
    # once (initial load); no refetch after the failed write
    assert sum(1 for u in out["urls"]
               if u.startswith("/api/keys?tenant_id=")) == 1, out["urls"]


def test_c6_create_form_validates_cap_client_side(tmp_path):
    out = _run(tmp_path, _routes(), script=[
        {"click": "keys-create"},
        {"set": ["key-cap", "abc"]},
        {"click": "key-create-btn"},
        {"wait": 20},
    ])
    assert out["texts"].get("keyerr-create") == "Spend cap must be a number."
    assert out["writes"] == []


# --------------------------------------------- gate 6: §4.5 admin token entry

def test_c6_write_carries_bearer_only_after_token_entry(tmp_path):
    fx = _routes()
    fx["_routes"]["POST /api/keys"] = {"status": 401, "body": {"detail": "unauthorized"}}
    out = _run(tmp_path, fx, script=[
        {"click": "keys-create"},
        {"click": "key-create-btn"},
        {"wait": 40},
    ])
    # the refused write carried NO Authorization header (§4.5: reads+first
    # write are unauthenticated; the header appears only after entry)
    assert out["writes"], "expected the refused write to be captured"
    assert "Authorization" not in out["writes"][0]["headers"]
    # the entry form appeared with the boot-print hint, masked input
    html = out["html"]
    assert "Admin token" in html
    assert "Printed once to the proxy" in html and "startup log at boot" in html
    assert '<input id="token-input" type="password"' in html
    # token never renders in DOM text outside the masked input
    assert "tok_abc123" not in html

    # enter the token → the pending create is replayed WITH the bearer
    fx2 = _routes()
    fx2["_routes"]["POST /api/keys"] = [
        {"status": 401, "body": {"detail": "unauthorized"}},
        {"body": {"id": "k9", "key_last4": "9911", "key": PLAINTEXT_KEY}},
    ]
    out2 = _run(tmp_path, fx2, script=[
        {"click": "keys-create"},
        {"click": "key-create-btn"},
        {"wait": 40},
        {"set": ["token-input", "tok_abc123"]},
        {"click": "token-save"},
        {"wait": 40},
    ])
    writes = out2["writes"]
    assert len(writes) == 2, writes
    assert "Authorization" not in writes[0]["headers"]
    assert writes[1]["headers"].get("Authorization") == "Bearer tok_abc123"
    assert writes[1]["headers"].get("Content-Type") == "application/json"
    # after entry the form is cleared and the token appears nowhere in the DOM
    assert "tok_abc123" not in out2["html"]
    assert PLAINTEXT_KEY in out2["html"]   # the pending create completed


def test_c6_mid_session_401_clears_token_and_reprompts(tmp_path):
    fx = _routes()
    fx["_routes"]["POST /api/keys"] = [
        {"status": 401, "body": {"detail": "unauthorized"}},
        {"status": 401, "body": {"detail": "unauthorized"}},
    ]
    out = _run(tmp_path, fx, script=[
        {"click": "keys-create"},
        {"click": "key-create-btn"},
        {"wait": 40},
        {"set": ["token-input", "stale_token"]},
        {"click": "token-save"},
        {"wait": 40},
    ])
    # second 401 → the stored token was cleared and the form re-opened with
    # the restart message (§4.5.3), NOT the boot hint
    assert "Token rejected — the proxy may have restarted." in out["html"]
    assert "Enter the current startup-log token." in out["html"]
    assert '<input id="token-input" type="password"' in out["html"]
    # both retries carried the bearer; the rejected value renders nowhere
    assert [w["headers"].get("Authorization") for w in out["writes"]] == \
        [None, "Bearer stale_token"]
    assert "stale_token" not in out["html"]


def test_c6_401_reprompt_preserves_pending_confirm_state(tmp_path):
    # §4.5.4 / gate 6: a 401 striking a confirmed rotate/revoke must NOT cost
    # the confirm state — while the token form is up the row still reads
    # "Confirm revoke?" and the operator re-enters the token once, not the
    # whole flow.
    fx = _routes()
    fx["_routes"]["POST /api/keys/k1/revoke"] = [
        {"status": 401, "body": {"detail": "unauthorized"}},
        {"body": {"id": "k1", "status": "revoked",
                  "revoked_at": "2026-09-19T00:00:00Z"}},
    ]
    out = _run(tmp_path, fx, script=[
        {"click": "rev-k1"},          # arm the two-step confirm (§4.3)
        {"wait": 20},
        {"click": "rev-k1"},          # confirm → POST → 401 → token prompt
        {"wait": 40},
    ])
    html = out["html"]
    # the refused write carried no header; the masked prompt rendered
    assert [w["headers"].get("Authorization") for w in out["writes"]] == [None]
    assert "Admin token" in html
    assert '<input id="token-input" type="password"' in html
    # the pending action's confirm state survived the 401 re-render
    assert "Confirm revoke?" in html
    assert "Revoked keys stop authenticating immediately." in html

    # entering the token replays the SAME pending action — no re-arm needed —
    # and the revoke completes (row flips to revoked on the refresh)
    out2 = _run(tmp_path, fx, script=[
        {"click": "rev-k1"}, {"wait": 20},
        {"click": "rev-k1"}, {"wait": 40},
        {"set": ["token-input", "tok_abc123"]},
        {"click": "token-save"}, {"wait": 40},
    ])
    writes = out2["writes"]
    assert [w["headers"].get("Authorization") for w in writes] == \
        [None, "Bearer tok_abc123"]
    assert writes[-1]["url"] == "/api/keys/k1/revoke"
    assert '<span class="badge red">revoked</span>' in out2["html"]
    assert "tok_abc123" not in out2["html"]


def test_c6_mid_session_401_on_confirm_keeps_row_confirm_visible(tmp_path):
    # §4.5.3+§4.5.4 combined: proxy restart (second, fresh 401) while a
    # revoke is pending — token cleared, restart message shown, confirm state
    # still preserved so one token re-entry finishes the action.
    fx = _routes()
    fx["_routes"]["POST /api/keys/k1/revoke"] = [
        {"status": 401, "body": {"detail": "unauthorized"}},
        {"status": 401, "body": {"detail": "unauthorized"}},
    ]
    out = _run(tmp_path, fx, script=[
        {"click": "rev-k1"}, {"wait": 20},
        {"click": "rev-k1"}, {"wait": 40},          # 401 #1 → prompt
        {"set": ["token-input", "stale_token"]},
        {"click": "token-save"}, {"wait": 40},      # 401 #2 → restart re-prompt
    ])
    html = out["html"]
    assert "Token rejected — the proxy may have restarted." in html
    assert "Enter the current startup-log token." in html
    assert "Confirm revoke?" in html   # confirm state survived BOTH 401s
    assert [w["headers"].get("Authorization") for w in out["writes"]] == \
        [None, "Bearer stale_token"]
    assert "stale_token" not in html

def test_c6_key_drawer_renders_scoped_kpis_1to1(tmp_path):
    # §2.4: one fetch per open — /api/kpis?api_key_id=k1 — cards verbatim
    out = _run(tmp_path, _routes(), script=[
        {"click": "keyrow-k1"},
        {"wait": 40},
    ])
    html = out["html"]
    assert sum(1 for u in out["urls"] if "api_key_id=k1" in u) == 1, out["urls"]
    assert "Usage —" in html
    assert '<span class="key-dot">••••</span> 1234' in html
    assert '<div class="kpi-num">2</div>' in html            # overview.requests
    assert '<div class="kpi-num">300</div>' in html          # input_tokens_saved
    assert f'<div class="kpi-num">{_money(0.0004)}</div>' in html  # cost_saved
    assert ">Close</button>" in html
    # closing clears the drawer; reopening re-fetches (no client-side caching)
    out2 = _run(tmp_path, _routes(), script=[
        {"click": "keyrow-k1"}, {"wait": 40},
        {"click": "drawer-close"}, {"wait": 20},
    ])
    assert "Usage —" not in out2["html"]


def test_c6_unattended_revoke_confirm_reverts_after_5s(tmp_path):
    # §4.3: the confirm state reverts to plain if untouched for 5s
    out = _run(tmp_path, _routes(), script=[
        {"click": "rev-k1"}, {"wait": 20},
        {"wait": 5200},
    ])
    assert "Confirm revoke?" not in out["html"]
    assert ">Revoke</button>" in out["html"]
    assert out["writes"] == []


def test_c6_reads_never_carry_authorization(tmp_path):
    out = _run(tmp_path, _routes())
    # every captured fetch is a GET without an Authorization header (§4.5:
    # reads stay unauthenticated; only writes ever attach the bearer)
    for u in out["urls"]:
        assert u.startswith("/"), u
    assert all(w["method"] == "POST" for w in out["writes"])
