"""AC-P6f: live tripwire loop over the request ledger.

The honesty guarantee is a LOOP, not a one-time certificate (product-spec
AC-P6f): production records ``dose_tier``, ``grounded_risk`` and the
AC-P6j ``envelope_shape`` scanner flag per request (plus the already-ledgered
realized output tokens), and this module evaluates two monitoring rules:

1. **Dose drift** — grounded-answer requests dosed at ``bounded`` whose
   realized input cut falls OUTSIDE the calibrated band. A red read here is
   the drift signal that re-triggers AC-P6c (re-calibration).
2. **Missed grounding** — requests whose content carries retrieval-envelope
   structure (per the AC-P6j shape scanner) but that were classified tier
   ``full`` or never classified at all, realizing a deep cut — the class of
   wrapper shapes the detector does not (yet) recognize, observed live.

The rules are PURE functions over ledger rows; the endpoint
(:func:`tripwire_endpoint`) is the only impure part (ledger fetch).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

PROXY_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROXY_ROOT / "benchmark" / "results"
DEFAULT_WINDOW_DAYS = 7

# Resolved-tier values that mean "the discriminator did NOT protect this
# request". "full" = classified ungrounded; NULL = the discriminator never
# ran (conciseness off, passthrough route) — both belong to the
# missed-grounding class the tripwire exists to catch.
_MISSED_TIERS = (None, "full")


def load_calibration_band(results_dir: Path | None = None) -> tuple[float, float] | None:
    """Load the calibrated ``bounded`` realized-cut band from the AC-P6c
    calibration artifact in ``benchmark/results/``.

    Returns ``(low, high)`` cut percentages, or None while no artifact
    exists (the tripwire then reports ``pending_calibration`` — it must not
    invent a band). Accepted shapes, tried in order:
    ``{"cut_pct_band": [lo, hi]}``, ``{"band": {"cut_pct": [lo, hi]}}``,
    ``{"cut_pct": [lo, hi]}``.
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
    for candidate in (
        data.get("cut_pct_band") if isinstance(data, dict) else None,
        (data.get("band") or {}).get("cut_pct")
        if isinstance(data, dict) and isinstance(data.get("band"), dict) else None,
        data.get("cut_pct") if isinstance(data, dict) else None,
    ):
        if (
            isinstance(candidate, (list, tuple))
            and len(candidate) == 2
            and all(isinstance(v, (int, float)) and v >= 0 for v in candidate)
            and candidate[0] <= candidate[1]
        ):
            return (float(candidate[0]), float(candidate[1]))
    return None


def realized_cut_pct(row: dict[str, Any]) -> float | None:
    """Realized input cut of a ledger row, in percent of the raw input."""
    before = row.get("input_tokens_before") or 0
    after = row.get("input_tokens_after") or 0
    if before <= 0:
        return None
    return max(0.0, 100.0 * (before - after) / before)


def evaluate_dose_drift(
    rows: list[dict[str, Any]],
    band: tuple[float, float] | None,
) -> dict[str, Any]:
    """Rule 1: grounded ``bounded`` rows cutting outside the calibrated band.

    Only rows whose realized cut EXCEEDS the band's upper edge flag — over-
    cutting grounded traffic is the fidelity risk the loop exists to catch
    (under-delivery is a savings miss, not a hazard, and is not flagged).
    """
    if band is None:
        return {"status": "pending_calibration", "flagged": [], "checked": 0}
    lo, hi = band
    flagged = []
    for row in rows:
        if row.get("dose_tier") != "bounded":
            continue
        if row.get("grounded_risk") not in ("bounded", "fidelity_critical"):
            continue
        cut = realized_cut_pct(row)
        if cut is not None and cut > hi:
            flagged.append({
                "id": row.get("id"),
                "ts": row.get("ts"),
                "model": row.get("model"),
                "grounded_risk": row.get("grounded_risk"),
                "realized_cut_pct": round(cut, 2),
                "band_pct": [lo, hi],
                "reason": "bounded_cut_above_band",
            })
    return {"status": "alert" if flagged else "clear", "flagged": flagged}


def evaluate_missed_grounding(
    rows: list[dict[str, Any]],
    deep_cut_pct: float,
) -> dict[str, Any]:
    """Rule 2: envelope-shaped content classified tier ``full`` (or never
    classified) realizing a deep cut."""
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
    band: tuple[float, float] | None = None,
    deep_cut_pct: float = 25.0,
) -> dict[str, Any]:
    """Compose the full AC-P6f report. Pure: rules over pre-fetched rows."""
    band = band if band is not None else load_calibration_band()
    dose_drift = evaluate_dose_drift(rows, band)
    missed = evaluate_missed_grounding(rows, deep_cut_pct)
    status = "red" if "alert" in (dose_drift["status"], missed["status"]) else (
        "pending" if dose_drift["status"] == "pending_calibration" else "green"
    )
    return {
        "status": status,
        "dose_drift": dose_drift,
        "missed_grounding": missed,
        "calibration_band_pct": list(band) if band else None,
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
    try:
        rows = fetch_tripwire_rows(days)
    except Exception as exc:  # noqa: BLE001 — ledger-unavailable is 503, not a crash
        return JSONResponse(
            {"error": "ledger unavailable", "detail": str(exc)}, status_code=503)
    return JSONResponse(tripwire_report(rows, deep_cut_pct=deep))


def _deep_cut_pct() -> float:
    from .config import get_settings

    return get_settings().tripwire_deep_cut_pct
