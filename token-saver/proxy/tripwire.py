"""AC-P6f: live tripwire loop over the request ledger.

The honesty guarantee is a LOOP, not a one-time certificate (product-spec
AC-P6f): production records ``dose_tier``, ``grounded_risk`` and the
AC-P6j ``envelope_shape`` scanner flag per request (plus the already-ledgered
realized output tokens), and this module evaluates two monitoring rules:

1. **Dose drift** — the live ``bounded`` grounded traffic's realized
   OUTPUT-token distribution drifting outside the band the AC-P6c
   calibration run measured on its bounded treatment arm. Production has NO
   per-request counterfactual answer, so per-request output cut cannot be
   computed (PM blocker ruling 2026-09-18); the honest instrument is
   DISTRIBUTIONAL: live bounded rows' mean output tokens vs the calibrated
   arm's mean CI. Drifting BELOW the calibrated floor = the dose cutting
   grounded answers harder than calibration showed safe — the fidelity
   hazard that re-triggers AC-P6c. The metric requires @product-manager
   ratification before it can read red
   (``tripwire_output_metric_ratified``; same pattern as
   ``grounded_calibration_green``).
2. **Missed grounding** — requests whose content carries retrieval-envelope
   structure (per the AC-P6j shape scanner) but that were classified tier
   ``full`` or never classified at all, realizing a deep INPUT cut — the
   class of wrapper shapes the detector does not (yet) recognize, observed
   live. (Input cut IS the correct unit here: the compressor did the
   cutting, and the hazard is lost input context.)

The rules are PURE functions over ledger rows; the endpoint
(:func:`tripwire_endpoint`) is the only impure part (ledger fetch).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROXY_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROXY_ROOT / "benchmark" / "results"
DEFAULT_WINDOW_DAYS = 7

# Resolved-tier values that mean "the discriminator did NOT protect this
# request". "full" = classified ungrounded; NULL = the discriminator never
# ran (conciseness off, passthrough route) — both belong to the
# missed-grounding class the tripwire exists to catch.
_MISSED_TIERS = (None, "full")

_GROUNDED_RISKS = ("bounded", "fidelity_critical")


def load_calibration_band(results_dir: Path | None = None) -> dict | None:
    """Load the AC-P6c calibration band artifact (PM blocker fix, 2026-09-18).

    Reads ``calibration_<model>_<stamp>.json`` as emitted by
    ``run_benchmark.py --emit-calibration`` (the harness is the ONLY writer;
    a bare ``benchmark_*.json`` never matches the glob and never arms the
    rule). Returns None while no valid artifact exists — the tripwire then
    reports ``pending_calibration`` and must not invent a band.

    Returned shape (``metric`` is stamped so the input/output unit mismatch
    class cannot recur silently)::

        {"artifact": <name>, "tier": "bounded", "metric": "output_tokens",
         "cut_pct_band": [lo, hi],              # output REDUCTION pct band
         "bounded_output_tokens": {"mean": m, "ci95_interval": [lo, hi],
                                   "n": k},
         "population_n": k}
    """
    d = results_dir if results_dir is not None else RESULTS_DIR
    if not d.is_dir():
        return None
    artifacts = sorted(d.glob("calibration*.json"))
    if not artifacts:
        return None
    try:
        data = json.loads(artifacts[-1].read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    # Contract: only the harness's emitted kind arms the rule. A stray file
    # that merely starts with "calibration" (or an artifact for another
    # tier/metric) stays inert.
    if (data.get("artifact_kind") != "ac_p6c_calibration"
            or data.get("tier") != "bounded"
            or data.get("metric") != "output_tokens"):
        return None
    band = data.get("cut_pct_band")
    arm = data.get("bounded_output_tokens")
    if not (isinstance(band, (list, tuple)) and len(band) == 2
            and all(isinstance(v, (int, float)) and v >= 0 for v in band)
            and band[0] <= band[1]):
        return None
    if not (isinstance(arm, dict)
            and isinstance(arm.get("mean"), (int, float))
            and isinstance(arm.get("ci95_interval"), (list, tuple))
            and len(arm["ci95_interval"]) == 2
            and all(isinstance(v, (int, float)) for v in arm["ci95_interval"])
            and arm["ci95_interval"][0] <= arm["ci95_interval"][1]):
        return None
    return {
        "artifact": artifacts[-1].name,
        "tier": data["tier"],
        "metric": "output_tokens",
        "cut_pct_band": [float(band[0]), float(band[1])],
        "bounded_output_tokens": {
            "mean": float(arm["mean"]),
            "ci95_interval": [float(arm["ci95_interval"][0]),
                              float(arm["ci95_interval"][1])],
            "n": arm.get("n"),
        },
        "population_n": (data.get("population") or {}).get("n"),
    }


def realized_cut_pct(row: dict[str, Any]) -> float | None:
    """Realized INPUT cut of a ledger row, in percent of the raw input.

    Input unit is correct for the missed-grounding rule (the compressor cut
    the context); it is NOT the dose-drift metric (see module docstring).
    """
    before = row.get("input_tokens_before") or 0
    after = row.get("input_tokens_after") or 0
    if before <= 0:
        return None
    return max(0.0, 100.0 * (before - after) / before)


def evaluate_dose_drift(
    rows: list[dict[str, Any]],
    band: dict | None,
    metric_ratified: bool = False,
    min_live_rows: int = 20,
) -> dict[str, Any]:
    """Rule 1: live bounded grounded OUTPUT distribution vs calibration arm.

    Distributional by necessity (no per-request counterfactual output in
    production — PM blocker ruling). Hazard direction: live mean output
    tokens BELOW the calibrated arm's 95% floor = the dose is cutting
    grounded answers harder than the calibrated safe band — re-trigger
    AC-P6c. Live mean ABOVE the ceiling is a savings miss, not a fidelity
    hazard, and is reported but never flagged red.
    """
    if band is None:
        return {"status": "pending_calibration", "flagged": [], "preview": [],
                "checked": 0}
    live = [
        r for r in rows
        if r.get("dose_tier") == "bounded"
        and r.get("grounded_risk") in _GROUNDED_RISKS
        and (r.get("output_tokens") or 0) > 0
    ]
    live_mean = (
        sum(r["output_tokens"] for r in live) / len(live) if live else None)
    floor, ceiling = band["bounded_output_tokens"]["ci95_interval"]
    out = {
        "metric": "bounded_output_tokens_distributional",
        "live_rows": len(live),
        "live_mean_output_tokens": (round(live_mean, 2)
                                    if live_mean is not None else None),
        "calibrated_floor": floor,
        "calibrated_ceiling": ceiling,
        "artifact": band.get("artifact"),
        "flagged": [],
    }
    preview = live_mean is not None and live_mean < floor
    if not metric_ratified:
        # The metric itself is a @product-manager ruling; until it lands the
        # rule must never read red — it reports what it WOULD flag.
        out["status"] = "pending_metric_ruling"
        out["preview_flag"] = bool(preview)
        out["preview_reason"] = (
            "bounded_output_below_calibration_floor" if preview else None)
        return out
    if len(live) < min_live_rows:
        out["status"] = "insufficient_live_rows"
        out["required"] = min_live_rows
        return out
    if preview and live_mean is not None:
        out["flagged"] = [{
            "reason": "bounded_output_below_calibration_floor",
            "live_mean_output_tokens": round(live_mean, 2),
            "calibrated_floor": floor,
            "live_rows": len(live),
        }]
    out["status"] = "alert" if out["flagged"] else "clear"
    return out


def evaluate_missed_grounding(
    rows: list[dict[str, Any]],
    deep_cut_pct: float,
) -> dict[str, Any]:
    """Rule 2: envelope-shaped content classified tier ``full`` (or never
    classified) realizing a deep INPUT cut."""
    flagged = []
    for row in rows:
        if not row.get("envelope_shape"):
            continue
        if row.get("dose_tier") not in _MISSED_TIERS:
            continue
        cut = realized_cut_pct(row)
        if cut is not None and cut >= deep_cut_pct:
            flagged.append({
                "id": row.get("id"),
                "ts": row.get("ts"),
                "model": row.get("model"),
                "dose_tier": row.get("dose_tier"),
                "realized_cut_pct": round(cut, 2),
                "reason": "envelope_shape_unprotected_deep_cut",
            })
    return {"status": "alert" if flagged else "clear", "flagged": flagged}


def tripwire_report(
    rows: list[dict[str, Any]],
    band: dict | None = None,
    deep_cut_pct: float = 25.0,
    metric_ratified: bool = False,
    min_live_rows: int = 20,
) -> dict[str, Any]:
    """Compose the full AC-P6f report. Pure: rules over pre-fetched rows."""
    band = band if band is not None else load_calibration_band()
    dose_drift = evaluate_dose_drift(rows, band, metric_ratified,
                                     min_live_rows)
    missed = evaluate_missed_grounding(rows, deep_cut_pct)
    alert = "alert" in (dose_drift["status"], missed["status"])
    status = "red" if alert else (
        "pending" if dose_drift["status"] != "clear" else "green")
    return {
        "status": status,
        "dose_drift": dose_drift,
        "missed_grounding": missed,
        "calibration_artifact": band.get("artifact") if band else None,
        "rows_scanned": len(rows),
    }


# --- Ledger fetch (the only impure part) -----------------------------------

_COLUMNS = (
    "id, ts, model, route, dose_tier, grounded_risk, envelope_shape, "
    "input_tokens_before, input_tokens_after, output_tokens"
)


def fetch_tripwire_rows(days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, Any]]:
    """Ledger rows for the tripwire window.

    Ledger selection mirrors stats.log_request: TOKEN_SAVER_PG_DSN set ->
    Postgres, otherwise the local SQLite ledger — deterministic, never
    reachability-probed. (db.get_pg_dsn RAISES on unset; the env check here
    is the explicit selection, not a probe.)
    """
    import os

    if os.environ.get("TOKEN_SAVER_PG_DSN"):
        return _fetch_rows_postgres(days)
    return _fetch_rows_sqlite(days)


def _fetch_rows_sqlite(days: int) -> list[dict[str, Any]]:
    import time

    from .stats import get_conn

    since = time.time() - days * 86400
    with get_conn() as conn:
        cur = conn.execute(
            f"SELECT {_COLUMNS} FROM requests"
            " WHERE ts >= ? ORDER BY ts DESC LIMIT 5000",
            (since,),
        )
        return [dict(r) for r in cur.fetchall()]


def _fetch_rows_postgres(days: int) -> list[dict[str, Any]]:
    import os

    import psycopg

    with psycopg.connect(os.environ["TOKEN_SAVER_PG_DSN"],
                         connect_timeout=3) as conn, \
            conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM requests"
            f" WHERE ts >= now() - (%s || ' days')::interval"
            " ORDER BY ts DESC LIMIT 5000",
            (str(days),),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


async def tripwire_endpoint(days: int = DEFAULT_WINDOW_DAYS):
    from fastapi.responses import JSONResponse

    if days < 1 or days > 365:
        return JSONResponse({"error": "days must be between 1 and 365"},
                            status_code=400)
    deep = _deep_cut_pct()
    ratified, min_rows = _drift_settings()
    try:
        rows = fetch_tripwire_rows(days)
    except Exception as exc:  # noqa: BLE001 — ledger-unavailable is 503, not a crash
        return JSONResponse(
            {"error": "ledger unavailable", "detail": str(exc)}, status_code=503)
    return JSONResponse(tripwire_report(
        rows, deep_cut_pct=deep, metric_ratified=ratified,
        min_live_rows=min_rows))


def _deep_cut_pct() -> float:
    from .config import get_settings

    return get_settings().tripwire_deep_cut_pct


def _drift_settings() -> tuple[bool, int]:
    from .config import get_settings

    s = get_settings()
    return s.tripwire_output_metric_ratified, s.tripwire_min_live_rows
