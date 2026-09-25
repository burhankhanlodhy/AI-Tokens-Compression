"""V2.2 runtime configuration layer (PM spec §3.2, t_d86fe22b).

Precedence (highest wins), fixed by the PM contract so no two cards
re-derive it:

  1. per-request control headers (x-token-saver-conciseness,
     x-token-saver-dose-pin — benchmark-only, handled at the call sites,
     unchanged)
  2. runtime overrides  -> Postgres ``app_settings`` when TOKEN_SAVER_PG_DSN
     is set, else a JSON file next to the SQLite ledger DB
  3. environment variables -> config.py ``Settings`` (unchanged behavior)
  4. built-in defaults     -> config.py ``Settings``

Design rules this module owns (all testable here, B2):

- ALLOWLIST: only names in ``RUNTIME_ALLOWED`` are writable at runtime.
  Unknown names -> :class:`UnknownSettingError`; known-but-not-runtime ->
  :class:`NotRuntimeConfigurableError`. Both are 400s at the API boundary,
  field-named, per B-10.
- WRITE-ONLY booleans: an override value must be a JSON boolean. Numbers,
  strings, dicts, nulls are refused — no runtime path can persist a
  credential, URL, model name, or threshold (PM §3.1).
- SOURCE TRUTH: ``effective()`` returns the winning source per name
  (``runtime`` | ``env`` | ``default``) — the UI renders THIS, never the
  raw overrides table alone (PM §3.2).
- DELETE REVERTS: removing an override reverts to env/default immediately
  (no restart, no cache to clear beyond the process-local overlay).
- In-flight safety (B4): the request path reads the effective value ONCE
  per request (``snapshot()``); this module never mutates a snapshot
  mid-request.
- Failure honesty: a Postgres/SQLite outage on READ degrades to
  env/default (the proxy must not stop serving traffic because the
  settings store is down) but logs loudly; a failure on WRITE raises so
  the API surfaces 503 — an unacknowledged toggle is a disconnected
  control (design-system §1.2), never a silent loss.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .config import get_settings

logger = logging.getLogger("token-saver.settings")

# PM §3.1: exactly these controls are runtime-configurable. Each is (a)
# lossless or independently benchmarked, (b) already switchable per
# deployment without ordering hazards, and (c) harmless mid-flight (the
# request path reads settings at request start). Adding a name here is a
# product decision, not a code convenience — the QA gate (test suite)
# pins this tuple.
RUNTIME_ALLOWED: tuple[str, ...] = (
    "l1_enabled",
    "tool_schema_minify",
    "tool_schema_cache_enabled",
    "tool_result_optimization",
    "tool_result_compression_enabled",
    "output_conciseness_enabled",
    "semantic_cache_enabled",
)

# PM §3.1 deployment-only inventory: surfaced read-only in Settings,
# NEVER writable at runtime. Kept adjacent to the allowlist so the two
# sets cannot silently drift apart (the tests assert disjointness).
DEPLOYMENT_ONLY: tuple[str, ...] = (
    "compression_enabled",
    "llmlingua_model",
    "compression_rate",
    "provider_routing",
    "provider_base_urls",
    "grounded_calibration_green",
    "allow_dose_pin",
    "tripwire_deep_cut_pct",
    "tripwire_output_metric_ratified",
    "tripwire_min_live_rows",
    "embedding_model",
    "embedding_dimensions",
    "semantic_cache_max_cosine_distance",
    "semantic_cache_ttl_seconds",
    "semantic_cache_max_response_bytes",
    "semantic_cache_hnsw_ef_search",
    "upstream_base_url",
    "upstream_timeout_seconds",
    "admin_token",
    "measurement_tag",
    "cache_enabled",
    "tool_result_max_tokens",
    "tool_schema_compression_enabled",
    "codebase_optimization_enabled",
    "codebase_max_file_lines",
    "codebase_dedupe_imports",
    "shell_output_filtering",
    "conciseness_min_user_chars",
    "disable_reasoning_by_default",
)

RUNTIME_SETTINGS_DB_TABLE = "app_settings"


class UnknownSettingError(ValueError):
    """The setting name does not exist in the declared settings surface."""


class NotRuntimeConfigurableError(ValueError):
    """The setting exists but is deployment-only (PM §3.1)."""


class InvalidSettingValueError(ValueError):
    """Override payloads must be JSON booleans (PM §3.1 safety ruling)."""


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or name not in RUNTIME_ALLOWED:
        if isinstance(name, str) and name in DEPLOYMENT_ONLY:
            raise NotRuntimeConfigurableError(
                f"{name} is deployment-only; set it via environment at boot."
            )
        raise UnknownSettingError(f"unknown setting name {name!r}")
    return name


def _validate_value(value: Any) -> bool:
    if not isinstance(value, bool):
        raise InvalidSettingValueError(
            "value must be a boolean (runtime controls are switches)."
        )
    return value


def _runtime_env(name: str) -> str | None:
    """The environment value of a runtime-allowed setting, if the env sets it.

    Mirrors pydantic-settings: field ``l1_enabled`` is env ``L1_ENABLED``.
    """
    env_name = name.upper()
    if env_name in os.environ:
        return os.environ[env_name]
    prefix = "TOKEN_SAVER_"
    # config.py's explicit-alias convention (measurement_tag) generalized:
    # only claim a TOKEN_SAVER_-prefixed variable when it is present, never
    # guess at aliases the Settings model does not declare.
    if prefix + env_name in os.environ:
        return os.environ[prefix + env_name]
    return None


class SettingsStore:
    """Persisted runtime overrides with env/default fallback (PM §3.2)."""

    def __init__(self, pg_dsn: str | None = None, sqlite_path: str | None = None):
        self._pg_dsn = pg_dsn
        self._sqlite_path = sqlite_path
        self._lock = threading.RLock()

    # ---- backend selection (explicit, same ledger policy as stats.py) ----

    def _dsn(self) -> str | None:
        if self._pg_dsn is not None:
            return self._pg_dsn
        return os.environ.get("TOKEN_SAVER_PG_DSN") or None

    def _sqlite_file(self) -> Path:
        if self._sqlite_path is not None:
            return Path(self._sqlite_path)
        db_path = get_settings().database_path
        return Path(db_path).parent / "settings.json"

    # ---- PG storage ----

    def _pg_connect(self):
        import psycopg

        return psycopg.connect(self._dsn(), connect_timeout=3)

    def _pg_load_overrides(self) -> dict[str, tuple[bool, str | None, str | None]]:
        """name -> (value, updated_at_iso, updated_by) for all override rows."""
        with self._pg_connect() as conn:
            rows = conn.execute(
                f"SELECT name, value, updated_at, updated_by "
                f"FROM {RUNTIME_SETTINGS_DB_TABLE}"
            ).fetchall()
        return {
            row[0]: (bool(row[1]), row[2].isoformat() if row[2] else None, row[3])
            for row in rows
        }

    def _pg_write(self, name: str, value: bool, updated_by: str) -> None:
        with self._pg_connect() as conn:
            conn.execute(
                f"INSERT INTO {RUNTIME_SETTINGS_DB_TABLE} (name, value, updated_by) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = now(), updated_by = EXCLUDED.updated_by",
                (name, value, updated_by),
            )

    def _pg_delete(self, name: str) -> bool:
        with self._pg_connect() as conn:
            cur = conn.execute(
                f"DELETE FROM {RUNTIME_SETTINGS_DB_TABLE} WHERE name = %s", (name,)
            )
            return cur.rowcount > 0

    # ---- SQLite/JSON storage (SQLite-only deployments, PM §3.2) ----

    def _json_load(self) -> dict[str, Any]:
        path = self._sqlite_file()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "runtime settings file %s unreadable (%s); ignoring overrides",
                path, exc,
            )
            return {}
        if not isinstance(raw, dict):
            logger.warning(
                "runtime settings file %s is not a JSON object; ignoring overrides",
                path,
            )
            return {}
        return raw

    def _json_write(self, overrides: dict[str, Any]) -> None:
        path = self._sqlite_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(overrides, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(path)

    # ---- public surface ----

    def load_overrides(self) -> dict[str, tuple[bool, str | None, str | None]]:
        """All persisted overrides, or {} when the store is unreachable.

        A read failure DEGRADES to env/default (logged loudly) — the proxy
        must keep serving traffic when the settings store is down (PM §4
        degraded-state rule); the settings API surfaces the store error on
        its own fetch via :meth:`read_overrides_strict`.
        """
        if self._dsn():
            try:
                return self._pg_load_overrides()
            except Exception as exc:  # noqa: BLE001 — degrade, never crash traffic
                logger.error("runtime settings store unavailable on read: %s", exc)
                return {}
        with self._lock:
            raw = self._json_load()
        out: dict[str, tuple[bool, str | None, str | None]] = {}
        for name, entry in raw.items():
            if name not in RUNTIME_ALLOWED or not isinstance(entry, dict):
                continue
            value = entry.get("value")
            if not isinstance(value, bool):
                continue
            out[name] = (value, entry.get("updated_at"), entry.get("updated_by"))
        return out

    def read_overrides_strict(self) -> dict[str, tuple[bool, str | None, str | None]]:
        """Like :meth:`load_overrides` but raises on store failure.

        The settings API uses this so a broken store is a 503 (loud), not a
        misleading "everything is at default" response.
        """
        if self._dsn():
            return self._pg_load_overrides()
        with self._lock:
            return self.load_overrides()  # JSON path: already strict enough

    def write_override(self, name: str, value: Any, updated_by: str = "admin") -> None:
        name = _validate_name(name)
        value = _validate_value(value)
        if not isinstance(updated_by, str) or not updated_by.strip():
            raise InvalidSettingValueError("updated_by must be a non-empty identity.")
        if self._dsn():
            self._pg_write(name, value, updated_by.strip())
            return
        with self._lock:
            overrides = self._json_load()
            overrides[name] = {
                "value": value,
                "updated_at": _utc_now_iso(),
                "updated_by": updated_by.strip(),
            }
            self._json_write(overrides)

    def delete_override(self, name: str) -> bool:
        """Remove an override; True when a row/file entry actually existed.

        Deleting a name that was never overridden is a no-op (idempotent
        revert), not an error.
        """
        name = _validate_name(name)
        if self._dsn():
            return self._pg_delete(name)
        with self._lock:
            overrides = self._json_load()
            if name not in overrides:
                return False
            del overrides[name]
            self._json_write(overrides)
            return True

    def effective(self, name: str) -> dict[str, Any]:
        """The effective value + winning source for one runtime setting.

        Source semantics (PM §3.2, AC-I1):
          - ``runtime`` — a persisted override wins
          - ``env``     — the environment sets the variable
          - ``default`` — the built-in config default applies
        """
        name = _validate_name(name)
        settings = get_settings()
        default = bool(getattr(settings, name))
        env_raw = _runtime_env(name)
        env_value: bool | None = None
        if env_raw is not None:
            env_value = _parse_bool(env_raw, name)
        overrides = self.load_overrides()
        if name in overrides:
            value, updated_at, updated_by = overrides[name]
            return {
                "name": name,
                "value": value,
                "source": "runtime",
                "category": "runtime_configurable",
                "default": default,
                "env": env_value,
                "updated_at": updated_at,
                "updated_by": updated_by,
            }
        if env_value is not None:
            return {
                "name": name,
                "value": env_value,
                "source": "env",
                "category": "runtime_configurable",
                "default": default,
                "env": env_value,
                "updated_at": None,
                "updated_by": None,
            }
        return {
            "name": name,
            "value": default,
            "source": "default",
            "category": "runtime_configurable",
            "default": default,
            "env": env_value,
            "updated_at": None,
            "updated_by": None,
        }

    def snapshot(self) -> dict[str, Any]:
        """Effective settings for ONE request (B4: read once, never re-read).

        The request path must call this once at request start; a runtime
        flip mid-request never alters an in-flight request because the
        snapshot is a plain dict handed to the caller.
        """
        return {name: self.effective(name)["value"] for name in RUNTIME_ALLOWED}

    def inventory(self) -> list[dict[str, Any]]:
        """Full settings surface for the UI: every runtime control plus the
        read-only deployment inventory (PM §5.2, design-system §6.5.3).

        Deployment-only values are returned with their category marker but
        the UI (and this payload) never includes secrets: ``admin_token``
        and ``measurement_tag`` render as presence indicators only.
        """
        items: list[dict[str, Any]] = [self.effective(name) for name in RUNTIME_ALLOWED]
        settings = get_settings()
        for name in DEPLOYMENT_ONLY:
            value = getattr(settings, name, None)
            if name in ("admin_token", "measurement_tag"):
                rendered: Any = None if value in (None, "") else "••••"
            elif isinstance(value, bool):
                rendered = value
            elif isinstance(value, (int, float)):
                rendered = value
            else:
                rendered = str(value)
            env_raw = _runtime_env(name) if name != "admin_token" else (
                "set" if os.environ.get("ADMIN_TOKEN") else None
            )
            items.append({
                "name": name,
                "value": rendered,
                "source": "env" if env_raw is not None else "default",
                "category": "deployment_only",
            })
        return items


def _parse_bool(raw: str, name: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    logger.warning(
        "env value %r for %s is not a boolean literal; treating as false",
        raw, name,
    )
    return False


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_store: SettingsStore | None = None
_store_lock = threading.Lock()


def get_settings_store() -> SettingsStore:
    """Process-wide store (backend pinned by the same env the ledger uses)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SettingsStore()
        return _store


def reset_settings_store() -> None:
    """Test hook: drop the cached store so env changes take effect."""
    global _store
    with _store_lock:
        _store = None
