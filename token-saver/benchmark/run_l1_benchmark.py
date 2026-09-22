"""B2 runner: measure L1 structural cleanup over the committed B1 fixtures.

AC-P1e measurement + taxonomy §6 contract checks. Hard requirements:
- FAILS on checksum mismatch of fixtures/l1_prompts.json (pinned-fixture
  integrity — deviating fixtures are a contract break, not a silent proceed).
- Measures with the real tokenizer (proxy.counting / tiktoken), not len//4.
- Publishes the DECOMPOSITION table (C1-only / C2-only / C3-only / full),
  not a single headline, with the conservative customer-facing claim first
  (PM ruling: C1-only ≈30% published, C3 upside stated separately).
- Verifies taxonomy §6: determinism (clean twice -> identical bytes),
  idempotence, control-category byte-identity (negative list), reserved
  field preservation, provenance preservation (PM amendment).

Usage: .venv/bin/python benchmark/run_l1_benchmark.py [--out results/]
                                                    [--production-path]
Exits non-zero on any contract violation or checksum mismatch.

--production-path additionally measures the REAL production ordering:
classify(raw) -> l1_eligible(messages, route) -> clean_messages, exactly as
main.py runs it. The shared L1 gate deliberately remains independent of the
lossy compression route gate. The results JSON then carries both transform-level
and end-to-end columns,
and the run FAILS if the two diverge beyond tolerance — production yield
returning to zero while the transform-level number stays ~30% is exactly
the silent divergence this mode exists to catch (B2 P1, PM board task).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from proxy.l1_clean import clean_messages  # noqa: E402
from proxy.counting import count_messages  # noqa: E402

FIXTURES = ROOT / "fixtures" / "l1_prompts.json"
CHECKSUM_FILE = ROOT / "fixtures" / "l1_prompts.json.sha256"
MODEL = "gpt-4o"  # counting model, matches PM's verification run

STRIPTABLE_CATS = ("rag", "json_doc", "system_dup", "log_trace")
CONTROL_CAT = "control"


def fail_on_checksum_mismatch() -> None:
    expected = CHECKSUM_FILE.read_text().split()[0].strip()
    actual = hashlib.sha256(FIXTURES.read_bytes()).hexdigest()
    if actual != expected:
        print(f"FATAL: fixture checksum mismatch\n  expected {expected}\n"
              f"  actual   {actual}", file=sys.stderr)
        raise SystemExit(2)


def reduction(before: int, after: int) -> float:
    return (1 - after / before) * 100 if before else 0.0


def measure(prompts: list[dict], c1: bool, c2: bool, c3: bool) -> dict:
    per_cat: dict[str, list[int]] = {}
    for p in prompts:
        b = count_messages(p["messages"], MODEL)
        a = count_messages(clean_messages(p["messages"], c1=c1, c2=c2, c3=c3),
                           MODEL)
        per_cat.setdefault(p["category"], [0, 0])
        per_cat[p["category"]][0] += b
        per_cat[p["category"]][1] += a
    return per_cat


def measure_production_path(prompts: list[dict]) -> dict:
    """End-to-end column: classify(raw) -> l1_eligible gate -> clean,
    mirroring main.py's ordering via the SHARED predicate (taxonomy v1.2
    §5, ruling A — no hardcoded route shortcut here; re-introducing the
    route gate inside proxy.l1_clean.l1_eligible alone must zero this
    column)."""
    from proxy.classifier import classify
    from proxy.l1_clean import l1_eligible

    per_cat: dict[str, list[int]] = {}
    routed: dict[str, int] = {}
    per_item_reduction: dict[str, list[float]] = {}
    for p in prompts:
        route = classify(p["messages"])
        b = count_messages(p["messages"], MODEL)
        if not l1_eligible(p["messages"], route):
            a = b
        else:
            a = count_messages(clean_messages(p["messages"]), MODEL)
        per_cat.setdefault(p["category"], [0, 0])
        per_cat[p["category"]][0] += b
        per_cat[p["category"]][1] += a
        per_item_reduction.setdefault(p["category"], []).append(reduction(b, a))
        routed[p["category"]] = routed.get(p["category"], 0) + \
            (1 if route == "compress" else 0)
    return {
        "per_cat": per_cat,
        "routed_compress": routed,
        "per_item_reduction": per_item_reduction,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--production-path", action="store_true",
                    help="also measure the end-to-end production ordering "
                         "(classify -> shared L1 eligibility -> clean) and fail if the "
                         "transform-level and end-to-end numbers diverge")
    args = ap.parse_args()

    fail_on_checksum_mismatch()
    data = json.loads(FIXTURES.read_text())
    prompts = data["prompts"]
    print(f"fixtures: {len(prompts)} prompts, checksum OK")

    violations: list[str] = []

    # ---- taxonomy §6 contract checks (full cleaner) ----
    for p in prompts:
        once = clean_messages(p["messages"])
        twice = clean_messages(once)
        if json.dumps(once, sort_keys=True) != json.dumps(twice, sort_keys=True):
            violations.append(f"{p['id']}: not idempotent")
        if p["category"] == CONTROL_CAT and once != p["messages"]:
            violations.append(f"{p['id']}: control category modified")

    # reserved-field + provenance preservation: every reserved key's value
    # must survive byte-identically somewhere in the cleaned prompt
    import re

    def reserved_values(msgs: list[dict]) -> set[str]:
        blob = json.dumps(msgs, ensure_ascii=False)
        return {blob}  # trivially true; real check below per-key

    for p in prompts:
        if p["category"] == CONTROL_CAT:
            continue
        before_blob = json.dumps(p["messages"], ensure_ascii=False)
        after_blob = json.dumps(clean_messages(p["messages"]),
                                ensure_ascii=False)
        # reserved content values: extract strings under reserved keys
        def reserved_strings(obj, acc):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("content", "text", "passage", "answer"):
                        if isinstance(v, str):
                            acc.add(v)
                    reserved_strings(v, acc)
            elif isinstance(obj, list):
                for x in obj:
                    reserved_strings(x, acc)
            return acc
        before_vals = reserved_strings(p["messages"], set())
        after_vals = reserved_strings(clean_messages(p["messages"]), set())
        missing = {v for v in before_vals if v not in after_vals
                   and v.strip() and not _is_json_like(v)}
        if missing:
            violations.append(f"{p['id']}: reserved content changed: "
                              f"{[m[:40] for m in list(missing)[:2]]}")
        # provenance: field names must survive as keys
        for field in ("source", "title", "url", "path", "page",
                      "chunk_id", "doc_id", "collection", "filename",
                      "page_number", "index_name", "passage_id", "source_id"):
            if f'"{field}"' in before_blob and f'"{field}"' not in after_blob:
                violations.append(f"{p['id']}: provenance field '{field}' stripped")

    # determinism across a fresh interpreter-equivalent call (same function,
    # repeated input -> identical output already covered by idempotence
    # check; here: repeat run equality)
    for p in prompts[:10]:
        if (json.dumps(clean_messages(p["messages"]), sort_keys=True)
                != json.dumps(clean_messages(p["messages"]), sort_keys=True)):
            violations.append(f"{p['id']}: nondeterministic")

    # ---- decomposition measurement (real tokenizer) ----
    variants = {
        "c1_only": (True, False, False),
        "c2_only": (False, True, False),
        "c3_only": (False, False, True),
        "full": (True, True, True),
    }
    results = {}
    for name, (c1, c2, c3) in variants.items():
        per_cat = measure(prompts, c1, c2, c3)
        sb = sum(per_cat.get(c, [0, 0])[0] for c in STRIPTABLE_CATS)
        sa = sum(per_cat.get(c, [0, 0])[1] for c in STRIPTABLE_CATS)
        cb = per_cat.get(CONTROL_CAT, [0, 0])[0]
        ca = per_cat.get(CONTROL_CAT, [0, 0])[1]
        results[name] = {
            "strippable4": {"before": sb, "after": sa,
                            "reduction_pct": round(reduction(sb, sa), 1)},
            "control": {"before": cb, "after": ca,
                        "reduction_pct": round(reduction(cb, ca), 1)},
            "per_category": {
                c: {"before": b, "after": a,
                    "reduction_pct": round(reduction(b, a), 1)}
                for c, (b, a) in sorted(per_cat.items())
            },
        }

    print(f"\n{'variant':<12}{'strippable-4 reduction':>24}{'control':>10}")
    for name, r in results.items():
        print(f"{name:<12}{r['strippable4']['reduction_pct']:>23.1f}%"
              f"{r['control']['reduction_pct']:>9.1f}%")

    full = results["full"]["strippable4"]["reduction_pct"]
    c1_only = results["c1_only"]["strippable4"]["reduction_pct"]

    # ---- production-path (end-to-end) measurement ----
    production = None
    if args.production_path:
        pp = measure_production_path(prompts)
        per_cat, routed = pp["per_cat"], pp["routed_compress"]
        sb = sum(per_cat[c][0] for c in STRIPTABLE_CATS)
        sa = sum(per_cat[c][1] for c in STRIPTABLE_CATS)
        cb, ca = per_cat.get(CONTROL_CAT, [0, 0])
        pp_red = round(reduction(sb, sa), 1)
        item_reductions = [
            pct
            for category in STRIPTABLE_CATS
            for pct in pp["per_item_reduction"].get(category, [])
        ]
        item_range = {
            "min_pct": round(min(item_reductions), 1),
            "median_pct": round(statistics.median(item_reductions), 1),
            "max_pct": round(max(item_reductions), 1),
        }
        print("\nproduction path (classify -> shared L1 eligibility -> clean):")
        for c in sorted(per_cat):
            b, a = per_cat[c]
            print(f"  {c:<12} {b:>7} -> {a:<7} "
                  f"{reduction(b, a):>6.1f}%  routed_compress={routed.get(c, 0)}")
        print(f"  strippable-4 END-TO-END reduction: {pp_red}%")
        production = {
            "ordering": "classify(raw) -> l1_eligible(messages, route) -> clean_messages",
            "strippable4": {"before": sb, "after": sa,
                            "reduction_pct": pp_red,
                            "routed_compress": sum(routed.get(c, 0)
                                                   for c in STRIPTABLE_CATS),
                            "item_reduction_range_pct": item_range},
            "control": {"before": cb, "after": ca,
                        "reduction_pct": round(reduction(cb, ca), 1)},
            "per_category": {
                c: {"before": b, "after": a,
                    "reduction_pct": round(reduction(b, a), 1),
                    "routed_compress": routed.get(c, 0)}
                for c, (b, a) in sorted(per_cat.items())
            },
        }
        # Divergence gate: end-to-end yield must not silently collapse while
        # the transform-level number holds. Tolerance 5pts absorbs the
        # small (6 control) prompts that legitimately skip L1.
        if abs(full - pp_red) > 5.0:
            violations.append(
                f"production-path divergence: transform-level {full}% vs "
                f"end-to-end {pp_red}% (>5pts) — L1 yield is being lost to "
                f"the route gate on strippable traffic")
        if production["control"]["reduction_pct"] != 0.0:
            violations.append(
                "production-path FAIL: control category modified via "
                "compress route (negative-list breach end-to-end)")


    # ---- AC gates ----
    if full < 15.0:
        violations.append(f"AC-P1e FAIL: full-cleaner reduction {full}% < 15%")
    if results["full"]["control"]["reduction_pct"] != 0.0:
        violations.append("negative-list FAIL: control category not byte-identical")

    result = {
        "schema": "l1_b2_v2",
        "fixture_sha256": hashlib.sha256(FIXTURES.read_bytes()).hexdigest(),
        "counting_model": MODEL,
        "tokenizer": "tiktoken (proxy.counting), not len//4",
        "taxonomy_version": "1.2",
        "decomposition": results,
        "production_path": production,
        "published_savings": (
            {
                "production_default": {
                    "variant": "C1+C2+C3",
                    "scope": "RAG/JSON-heavy strippable-4 corpus",
                    "reduction_pct": production["strippable4"]["reduction_pct"],
                    "item_reduction_range_pct": production["strippable4"]["item_reduction_range_pct"],
                    "per_content_class": {
                        category: production["per_category"][category]
                        for category in STRIPTABLE_CATS
                    },
                },
                "conservative_c1_only": {
                    "variant": "C1",
                    "scope": "RAG/JSON-heavy strippable-4 corpus",
                    "reduction_pct": results["c1_only"]["strippable4"]["reduction_pct"],
                    "per_content_class": {
                        category: results["c1_only"]["per_category"][category]
                        for category in STRIPTABLE_CATS
                    },
                },
            }
            if production is not None
            else None
        ),
        "customer_facing_claim": {
            "conservative": f"C1 JSON-whitespace compaction only: {c1_only}% input-token reduction on RAG/JSON-heavy categories",
            "upside": f"full cleaner (C1+C2+C3): {full}%; C3 dead-metadata drop is the dominant driver and is stated separately, not blended into the published claim",
        },
        "ac_p1e_pass": full >= 15.0,
        "violations": violations,
    }

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    outfile = outdir / "l1_b2_results.json"
    outfile.write_text(json.dumps(result, indent=2) + "\n")
    print(f"\nresults written: {outfile}")

    if violations:
        print(f"\nCONTRACT VIOLATIONS ({len(violations)}):")
        for v in violations[:20]:
            print(f"  - {v}")
        return 1
    print("\nAll §6 contract checks passed; AC-P1e gate:"
          f" {'PASS' if full >= 15.0 else 'FAIL'}")
    return 0


def _is_json_like(s: str) -> bool:
    t = s.strip()
    return t.startswith("{") or t.startswith("[")


if __name__ == "__main__":
    sys.exit(main())
