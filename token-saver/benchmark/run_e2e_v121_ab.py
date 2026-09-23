"""V1.2.1 integration E2E A/B: v1.2.0 baseline proxy vs merged v1.2.1 candidate.

Design (mirrors the T1-QA harness contract):
- Pinned corpus: benchmark/fixtures/e2e_corpus.json + .sha256 (refuses to run
  on mismatch — anti-cherry-picking).
- Two REAL proxies, spawned from their own checkouts on ephemeral ports,
  SQLite ledgers, provider routing off (single legacy upstream):
    baseline  : /tmp/v120-baseline (tag v1.2 == 3cb9dce)
    treatment : this repo working tree (integration/v1.2.1-candidate)
- Every scenario is sent through BOTH proxies with the SAME model and key
  (live upstream, BYOK from .env), temperature 0, k samples per arm.
- Reduction is measured on the upstream-reported usage.prompt_tokens per
  request, baseline vs treatment, aggregated ratio-of-sums per segment and
  overall. No synthetic token counts anywhere.
- Protocol integrity: treatment responses must parse, must be status 200,
  and tool-call scenarios must still carry a parsable tool_calls envelope.
- Ledger attribution: treatment SQLite rows must carry tool_compression_saved
  and schema_bytes_saved columns >= 0 and status 200 for every request.

Usage:
  .venv/bin/python benchmark/run_e2e_v121_ab.py --k 2
Exits non-zero on any gate violation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TREATMENT = Path(__file__).resolve().parent.parent
BASELINE = Path("/tmp/v120-baseline/token-saver")
CORPUS = TREATMENT / "benchmark" / "fixtures" / "e2e_corpus.json"
CHECKSUM_FILE = CORPUS.with_suffix(".json.sha256")
OUT_DIR = TREATMENT / "benchmark" / "results"

SEGMENT_GATES = {
    # Re-baselined v1.2.1 gates; whole-prompt ratio-of-sums vs v1.2.0.
    "tool": 8.0,
    "codebase": 35.0,
    "schema": 20.0,
    "result": 25.0,
}
OVERALL_GATE = 25.0
CONTROL_MAX_REGRESSION_PCT = 5.0  # control may not shrink by more than 5%

# Harness-observed upstream 404s (sticky-404 retry ladder). Each observed 404
# corresponds to exactly one non-200 row in that arm's proxy ledger, enabling
# exact row-count reconciliation (surfaced requests + observed 404s on the
# treatment arm = treatment ledger rows).
OBSERVED_404S = {"baseline": 0, "treatment": 0}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url: str, proc, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise RuntimeError(f"proxy died at startup:\n{out[-2000:]}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"proxy not healthy at {url}")


def chat(url: str, key: str, payload: dict, timeout: float = 180.0) -> dict:
    req = urllib.request.Request(
        url + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def chat_with_404_retry(url: str, key: str, payload: dict, pad: int = 0,
                        timeout: float = 180.0) -> dict:
    """chat() with the relay's sticky-404 workaround.

    api.oneprovider.dev caches errors per exact request-body content: a body
    that once 404'd ("Resource not found") keeps 404ing byte-identical, while
    a one-character mutation of the same content returns 200. When `pad` > 0,
    a content-neutral prefix of that many 'Q' chars is added to the last
    message so the relay sees a fresh cache key. Both arms get the same pad,
    and token accounting comes from upstream usage, so the comparison stays
    apples-to-apples.

    NOTE: the proxy's transforms keep the message tail, so the pad survives
    into the forwarded body and each successive pad produces a distinct
    upstream body. Pads escalate within a request until one lands on an
    unpoisoned cache key.
    """
    p = json.loads(json.dumps(payload))
    if pad:
        p["messages"][-1]["content"] = "Q" * pad + p["messages"][-1]["content"]
    return chat(url, key, p, timeout=timeout)


def chat_with_404_escalation(url: str, key: str, payload: dict,
                             base_pad: int = 0, max_escalation: int = 4,
                             timeout: float = 180.0) -> dict:
    """Send through the proxy, escalating the pad while upstream 404s.

    The relay serves sticky per-body 404s, and a poisoned hash can cover
    consecutive pad values; escalate until we get a 200 (or run out of rungs,
    which then surfaces the last HTTPError).
    """
    last_err: urllib.error.HTTPError | None = None
    for extra in range(max_escalation + 1):
        pad = base_pad + extra
        try:
            return chat_with_404_retry(url, key, payload, pad=pad, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            last_err = e
            time.sleep(0.5)
    assert last_err is not None
    raise last_err


def chat_with_pad_returned(url: str, key: str, payload: dict,
                           base_pad: int = 0, max_escalation: int = 4,
                           timeout: float = 180.0,
                           arm: str = "treatment") -> tuple[dict, int]:
    """chat_with_404_escalation that also returns the pad that succeeded.

    QA accounting: the pad count per request is the retry surface of the
    relay's sticky-404 workaround and must be reported, not swallowed.
    Every 404 the harness observes is counted per arm in OBSERVED_404S; each
    such attempt also produces exactly one non-200 row in that arm's proxy
    ledger, which is how the ledger row count is reconciled exactly.
    """
    last_err: urllib.error.HTTPError | None = None
    for extra in range(max_escalation + 1):
        pad = base_pad + extra
        try:
            return chat_with_404_retry(url, key, payload, pad=pad,
                                       timeout=timeout), pad
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            OBSERVED_404S[arm] += 1
            last_err = e
            time.sleep(0.5)
    assert last_err is not None
    raise last_err


def preflight(scenarios: list[dict], base_url: str, key: str, model: str,
              max_pad: int = 5) -> None:
    """Refuse to start the paid run on scenarios the relay refuses outright."""
    for s in scenarios:
        payload = {"model": model, "temperature": 0, "max_tokens": 5,
                   "messages": s["messages"]}
        if s.get("tools"):
            payload["tools"] = s["tools"]
        last_err = None
        for pad in range(max_pad + 1):
            try:
                chat_with_404_retry(base_url, key, payload, pad=pad, timeout=120)
                break
            except urllib.error.HTTPError as e:
                last_err = e.code
                if e.code != 404:
                    break
                time.sleep(0.5)
        else:
            sys.exit(
                f"preflight: scenario {s['id']} upstream-refused "
                f"(HTTP {last_err} on {max_pad + 1} padded variants) — "
                f"regenerate the corpus filler or switch model")


def spawn_proxy(root: Path, port: int, tmp: Path, label: str):
    env = {**os.environ,
           "DATABASE_PATH": str(tmp / f"stats_{label}.db"),
           "CACHE_ENABLED": "true",
           "PROVIDER_ROUTING": "false"}
    # Both arms MUST talk to the same upstream. The baseline checkout may have
    # no .env of its own, in which case its settings default to
    # https://openrouter.ai/api/v1 and every forwarded request 401s there.
    env["UPSTREAM_BASE_URL"] = re.search(
        r"UPSTREAM_BASE_URL=(\S+)", (TREATMENT / ".env").read_text()).group(1)
    env.pop("TOKEN_SAVER_PG_DSN", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "proxy.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(root), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc


def ledger_rows(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
        select = ["id", "status", "route"]
        if "tool_compression_saved" in cols:
            select.append("tool_compression_saved")
        if "schema_bytes_saved" in cols:
            select.append("schema_bytes_saved")
        if "schema_cache_hit" in cols:
            select.append("schema_cache_hit")
        return [dict(r) for r in conn.execute(
            f"SELECT {', '.join(select)} FROM requests ORDER BY id")]
    finally:
        conn.close()


def load_corpus(corpus_path: Path) -> tuple[list[dict], str]:
    """Read a fixture only when its adjacent SHA-256 pin matches."""
    corpus_path = Path(corpus_path)
    checksum_file = corpus_path.with_suffix(corpus_path.suffix + ".sha256")
    digest = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    expected = checksum_file.read_text().split()[0].strip()
    if digest != expected:
        raise ValueError(f"corpus checksum mismatch: {digest} != {expected}")
    scenarios = json.loads(corpus_path.read_text())
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("corpus must be a non-empty scenario list")
    return scenarios, digest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, default=CORPUS,
                    help="checksum-pinned corpus JSON (default: mixed integration corpus)")
    ap.add_argument("--k", type=int, default=2,
                    help="samples per arm per scenario")
    ap.add_argument("--model", default="gemini-3.5-flash")
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    try:
        scenarios, digest = load_corpus(args.corpus)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.exit(str(exc))

    env_text = (TREATMENT / ".env").read_text()
    key = re.search(r"OPENROUTER_API_KEY=(\S+)", env_text).group(1)
    upstream = re.search(r"UPSTREAM_BASE_URL=(\S+)", env_text).group(1)

    tmp = Path(tempfile.mkdtemp(prefix="e2e_v121_"))
    port_base, port_treat = free_port(), free_port()
    pb = spawn_proxy(BASELINE, port_base, tmp, "baseline")
    pt = spawn_proxy(TREATMENT, port_treat, tmp, "treatment")
    base_url = f"http://127.0.0.1:{port_base}"
    treat_url = f"http://127.0.0.1:{port_treat}"

    results: list[dict] = []
    try:
        wait_health(base_url + "/health", pb)
        wait_health(treat_url + "/health", pt)
        print(f"proxies up: baseline :{port_base} treatment :{port_treat}")

        # refuse to burn the paid run on bodies the upstream sticky-404s
        preflight(scenarios, base_url, key, args.model)

        for s in scenarios:
            payload = {"model": args.model, "temperature": 0,
                       "max_tokens": 400, "messages": s["messages"]}
            if s.get("tools"):
                payload["tools"] = s["tools"]
            for arm, url in (("baseline", base_url), ("treatment", treat_url)):
                for k in range(args.k):
                    row = {"id": s["id"], "segment": s["segment"], "arm": arm, "k": k}
                    pads_used = 0
                    try:
                        resp, pads_used = chat_with_pad_returned(
                            url, key, payload, base_pad=k, arm=arm)
                        usage = resp.get("usage") or {}
                        row["status"] = 200
                        row["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
                        msg = resp["choices"][0]["message"]
                        row["has_tool_calls"] = bool(msg.get("tool_calls"))
                        row["finish"] = resp["choices"][0].get("finish_reason")
                    except urllib.error.HTTPError as e:
                        row["status"] = e.code
                        row["prompt_tokens"] = 0
                        body = e.read().decode()[:300]
                        row["error"] = body
                    row["pad"] = pads_used
                    results.append(row)
            done = len([r for r in results if r["id"] == s["id"] and "prompt_tokens" in r])
            print(f"  {s['id']}: {done}/{args.k * 2} ok")
        # sanity: upstream reachable at all
        ok = [r for r in results if r.get("status") == 200 and r.get("prompt_tokens")]
        if len(ok) < len(results) * 0.9:
            sys.exit(f"too many failed upstream calls: {len(ok)}/{len(results)} ok")

        # aggregate ratio-of-sums per segment
        summary = {"generated_at": datetime.now(timezone.utc).isoformat(),
                   "model": args.model, "k": args.k,
                   "corpus_sha256": digest, "segments": {}, "protocol": {}}
        totals = {"baseline": 0, "treatment": 0}
        for seg in sorted({s["segment"] for s in scenarios}):
            seg_rows = [r for r in results
                        if r["segment"] == seg and r.get("status") == 200]
            b = sum(r["prompt_tokens"] for r in seg_rows if r["arm"] == "baseline")
            t = sum(r["prompt_tokens"] for r in seg_rows if r["arm"] == "treatment")
            totals["baseline"] += b
            totals["treatment"] += t
            summary["segments"][seg] = {
                "baseline_prompt_tokens": b, "treatment_prompt_tokens": t,
                "reduction_pct": round(100 * (b - t) / b, 2) if b else 0.0,
                "requests": len(seg_rows),
            }
        summary["overall"] = {
            "baseline_prompt_tokens": totals["baseline"],
            "treatment_prompt_tokens": totals["treatment"],
            "reduction_pct": round(
                100 * (totals["baseline"] - totals["treatment"]) / totals["baseline"], 2),
        }

        # protocol integrity on the treatment arm
        t_rows = [r for r in results if r["arm"] == "treatment"]
        b_rows = [r for r in results if r["arm"] == "baseline"]
        # Per-scenario paired results (QA deliverable): one row per scenario
        # with both arms' prompt tokens, so segment aggregates are auditable.
        per_scenario = []
        for s in scenarios:
            brow = next((r for r in b_rows if r["id"] == s["id"]
                         and r.get("status") == 200 and r.get("prompt_tokens")), None)
            trow = next((r for r in t_rows if r["id"] == s["id"]
                         and r.get("status") == 200 and r.get("prompt_tokens")), None)
            per_scenario.append({
                "id": s["id"], "segment": s["segment"],
                "baseline_prompt_tokens": brow["prompt_tokens"] if brow else None,
                "treatment_prompt_tokens": trow["prompt_tokens"] if trow else None,
                "reduction_pct": (
                    round(100 * (brow["prompt_tokens"] - trow["prompt_tokens"])
                          / brow["prompt_tokens"], 2)
                    if brow and trow and brow["prompt_tokens"] else None),
                "baseline_pad": brow.get("pad") if brow else None,
                "treatment_pad": trow.get("pad") if trow else None,
            })
        summary["per_scenario"] = per_scenario

        # Tool-call protocol parity: on tool-carrying scenarios the
        # treatment's 200 responses must preserve the tool_calls envelope at
        # least as often as the baseline arm does for the same
        # scenario/sample. The model legitimately may answer without calling
        # tools (e.g. result/schema scenarios where a tool exists but the
        # question is answerable directly) — that is model behavior, present
        # in BOTH arms, and not proxy corruption. Corruption signature is:
        # treatment has_tool_calls strictly LESS often than baseline.
        tool_scenarios = {s["id"] for s in scenarios
                          if s["segment"] in ("result", "tool") or s.get("tools")}
        t_tool = [r for r in t_rows if r["id"] in tool_scenarios
                  and r.get("status") == 200]
        # baseline 200 rows by (id, k)
        b_by_idk = {(r["id"], r["k"]): r for r in b_rows
                    if r["id"] in tool_scenarios and r.get("status") == 200}
        preserved, lost = [], []
        for r in t_tool:
            b = b_by_idk.get((r["id"], r["k"]))
            if r.get("has_tool_calls"):
                preserved.append(r)
            elif b is None or not b.get("has_tool_calls"):
                preserved.append(r)  # model chose no tool call in BOTH arms
            else:
                lost.append({"id": r["id"], "k": r["k"]})
        summary["protocol"] = {
            "treatment_status_200": len([r for r in t_rows if r.get("status") == 200]),
            "treatment_total": len(t_rows),
            "treatment_404_retries": len([r for r in t_rows if r.get("status") == 404]),
            "baseline_404_retries": len([r for r in b_rows if r.get("status") == 404]),
            "observed_404s_total": dict(OBSERVED_404S),
            "tool_call_200s_treatment": len(t_tool),
            "tool_call_200s_treatment_preserved": len(preserved),
            "tool_call_parity_losses": lost,
            "tool_call_scenarios_preserved": len(lost) == 0,
            "per_request_pads": {f"{r['id']}:{r['arm']}:{r['k']}": r.get("pad")
                                 for r in results},
        }

        # ledger attribution from the treatment proxy's SQLite
        rows = ledger_rows(str(tmp / "stats_treatment.db"))
        # Expected row count: every surfaced request logs exactly one row on
        # success; every harness-observed 404 (sticky-404 pad ladder) logs
        # exactly one non-200 row. OBSERVED_404S is counted at the harness
        # for BOTH arms' ladders, but the ledger read here is the treatment
        # proxy's DB, so only treatment-arm 404s belong to this count.
        # The harness records per-arm 404s in results rows (status 404 never
        # surfaces because escalation exhausts only on persistent failure, in
        # which case the run aborts) — hence: treatment 404 rows =
        # observed 404s on the treatment URL. Track them per arm:
        t_404s = OBSERVED_404S["treatment"]
        expected_rows = (len([r for r in t_rows if r.get("status") == 200])
                         + t_404s)
        status_200_rows = [r for r in rows if r["status"] == 200]
        summary["ledger"] = {
            "rows": len(rows),
            "expected_rows": expected_rows,
            "row_count_matches_requests": len(rows) == expected_rows,
            "status_200_rows": len(status_200_rows),
            "non_200_rows": len(rows) - len(status_200_rows),
            "treatment_observed_404s": t_404s,
            "non_200_note": ("non-200 ledger rows are the proxy's own log of "
                             "internal sticky-404 retry attempts (pad ladder)"),
            "has_tool_compression_saved": "tool_compression_saved" in rows[0] if rows else False,
            "has_schema_columns": "schema_bytes_saved" in rows[0] if rows else False,
            "tool_compression_saved_total": sum(
                r.get("tool_compression_saved", 0) for r in rows),
            "schema_bytes_saved_total": sum(
                r.get("schema_bytes_saved", 0) for r in rows),
        }

        tool_segment_ids = sorted({s["id"] for s in scenarios
                                   if s["segment"] == "tool"})
        tool_baseline_tokens = sum(
            r["prompt_tokens"] for r in results
            if r["segment"] == "tool" and r["arm"] == "baseline"
            and r.get("status") == 200)
        tool_saved = summary["ledger"]["tool_compression_saved_total"]
        summary["tool_segment_publication"] = {
            "as_shipped_whole_prompt": summary["segments"].get("tool"),
            "isolated_tool_compression_saved_tokens": tool_saved,
            "isolated_tool_compression_contribution_pct_of_baseline_prompt": (
                round(100 * tool_saved / tool_baseline_tokens, 2)
                if tool_baseline_tokens else None),
            "population_scenario_ids": tool_segment_ids,
            "corpus_sha256": digest,
            "isolated_metric_source": "treatment ledger tool_compression_saved; "
                                      "proxy-estimated tokens, not upstream usage",
        }

        # gates
        segs = summary["segments"]
        gates = {}
        for seg, floor in SEGMENT_GATES.items():
            if seg in segs:
                gates[f"{seg}_ge_{floor}"] = segs[seg]["reduction_pct"] >= floor
        if "control" in segs:
            ctrl = segs["control"]
            gates["control_not_shrunk"] = (
                ctrl.get("reduction_pct", 0) >= -CONTROL_MAX_REGRESSION_PCT)
        if {"tool", "codebase", "schema", "result"}.issubset(segs):
            gates[f"overall_ge_{OVERALL_GATE}"] = (
                summary["overall"]["reduction_pct"] >= OVERALL_GATE)
        gates["protocol_ok"] = (
            summary["protocol"]["treatment_status_200"]
            == summary["protocol"]["treatment_total"]
            and summary["protocol"]["tool_call_scenarios_preserved"])
        gates["ledger_ok"] = (
            summary["ledger"]["row_count_matches_requests"]
            and summary["ledger"]["status_200_rows"]
            == summary["protocol"]["treatment_status_200"]
            and summary["ledger"]["has_tool_compression_saved"]
            and summary["ledger"]["has_schema_columns"])
        summary["gates"] = gates
        summary["verdict"] = "PASS" if all(gates.values()) else "FAIL"

        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / (
            f"e2e_v121_ab_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
        out.write_text(json.dumps(summary, indent=1) + "\n")

        print(json.dumps(summary["segments"], indent=1))
        print("overall:", summary["overall"])
        print("protocol:", summary["protocol"])
        print("ledger:", summary["ledger"])
        print("tool_segment_publication:", summary["tool_segment_publication"])
        print("gates:", gates)
        print("verdict:", summary["verdict"], "->", out)
        if summary["verdict"] != "PASS":
            sys.exit(1)
    finally:
        for p in (pb, pt):
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
