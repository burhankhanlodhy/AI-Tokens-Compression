"""Incremental Diff Context Protocol primitives and Postgres file-version ledger.

Callers pass authenticated tenant/API-key/session scope and exact file bytes.
The module does not infer file paths from arbitrary conversation text. Returned
records are advisory context; callers must apply diffs only to the named base
version. The deployment flag remains off by default in Settings.
"""
from __future__ import annotations

import difflib
import hashlib
import posixpath
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class FileScope:
    tenant_id: str
    api_key_id: str
    session_id: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            self.tenant_id, self.api_key_id, self.session_id
        )):
            raise ValueError("tenant, API-key, and session scope are required")


@dataclass(frozen=True)
class FileVersion:
    canonical_path: str
    version_id: str
    content: bytes
    content_sha256: str


@dataclass(frozen=True)
class FileRead:
    kind: str  # full | unchanged | diff | deleted
    canonical_path: str
    version_id: str | None
    base_version_id: str | None
    content: bytes | None = None
    diff: str | None = None
    reason: str = ""
    new_version: FileVersion | None = None


class _Connection(Protocol):
    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any: ...


def canonicalize_path(path: str) -> str:
    """Normalize POSIX file paths and reject relative traversal above root."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("path must be a non-empty text path")
    normalized = posixpath.normpath(path)
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
        raise ValueError("path escapes its root")
    return normalized


def _new_version(path: str, content: bytes) -> FileVersion:
    return FileVersion(
        canonical_path=path,
        version_id=str(uuid.uuid4()),
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def prepare_file_read(
    *,
    scope: FileScope,
    path: str,
    content: bytes | None,
    current: FileVersion | None,
    expected_base_version_id: str | None = None,
    full_context: bool = False,
    max_diff_bytes: int = 16_384,
    max_diff_ratio: float = 0.70,
    validate_diff: Callable[[str], bool] | None = None,
) -> FileRead:
    """Build a conservative full/unchanged/diff record without mutating storage.

    An expected base is an optimistic concurrency token. A mismatch always
    returns full content and is never represented as an applicable diff.
    Deletion is an explicit tombstone; rename detection is intentionally not
    attempted, so a new path receives an ordinary full first read.
    """
    del scope  # Scope validation is enforced by FileScope and the store query.
    canonical = canonicalize_path(path)
    if content is None:
        return FileRead("deleted", canonical, None,
                        current.version_id if current else None,
                        reason="deletion_requires_full_context")
    if not isinstance(content, bytes):
        raise TypeError("file content must be bytes or None")
    version = _new_version(canonical, content)
    if current is None or current.canonical_path != canonical:
        return FileRead("full", canonical, version.version_id, None,
                        content=content, reason="first_read", new_version=version)
    if full_context:
        return FileRead("full", canonical, version.version_id, current.version_id,
                        content=content, reason="full_context_requested", new_version=version)
    if content == current.content:
        return FileRead("unchanged", canonical, current.version_id,
                        current.version_id, reason="content_unchanged", new_version=current)
    if (expected_base_version_id is not None
            and expected_base_version_id != current.version_id):
        return FileRead("full", canonical, version.version_id, current.version_id,
                        content=content, reason="stale_base", new_version=version)
    try:
        old_text = current.content.decode("utf-8")
        new_text = content.decode("utf-8")
    except UnicodeDecodeError:
        return FileRead("full", canonical, version.version_id, current.version_id,
                        content=content, reason="ambiguous_content", new_version=version)
    lines = list(difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{canonical}", tofile=f"b/{canonical}", lineterm="\n",
    ))
    diff = "".join(lines)
    if not diff:
        return FileRead("full", canonical, version.version_id, current.version_id,
                        content=content, reason="ambiguous_diff", new_version=version)
    diff_size = len(diff.encode("utf-8"))
    if diff_size > max_diff_bytes or diff_size >= len(content) or (
        len(content) >= 128 and diff_size >= int(len(content) * max_diff_ratio)
    ):
        return FileRead("full", canonical, version.version_id, current.version_id,
                        content=content, reason="diff_too_large", new_version=version)
    if validate_diff is not None:
        try:
            valid = validate_diff(diff)
        except Exception:
            valid = False
        if not valid:
            return FileRead("full", canonical, version.version_id, current.version_id,
                            content=content, reason="validation_failed", new_version=version)
    return FileRead("diff", canonical, version.version_id, current.version_id,
                    diff=diff, reason="changed", new_version=version)


class IDCPStore:
    """Append-only Postgres ledger adapter; call inside a transaction.

    A transaction-scoped advisory lock serializes writes for one tenant/key/
    session/path, including the initial read when no version row exists yet.
    """

    def __init__(self, connection: _Connection, *, ttl_seconds: int = 86_400):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.connection = connection
        self.ttl_seconds = ttl_seconds

    def read(
        self,
        scope: FileScope,
        path: str,
        content: bytes | None,
        *,
        enabled: bool = False,
        expected_base_version_id: str | None = None,
        full_context: bool = False,
        max_diff_bytes: int = 16_384,
        validate_diff: Callable[[str], bool] | None = None,
    ) -> FileRead:
        canonical = canonicalize_path(path)
        if not enabled:
            return prepare_file_read(scope=scope, path=canonical, content=content,
                                     current=None)
        if content is None:
            current = self._latest(scope, canonical, lock=False)
            return prepare_file_read(scope=scope, path=canonical, content=None,
                                     current=current)
        # Serializes same-scope/path changes across concurrent transactions.
        lock_key = "\x1f".join((scope.tenant_id, scope.api_key_id,
                                scope.session_id, canonical))
        self.connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
        )
        current = self._latest(scope, canonical, lock=True)
        result = prepare_file_read(
            scope=scope, path=canonical, content=content, current=current,
            expected_base_version_id=expected_base_version_id,
            full_context=full_context, max_diff_bytes=max_diff_bytes,
            validate_diff=validate_diff,
        )
        version = result.new_version
        if version is not None and (current is None or version.version_id != current.version_id):
            self.connection.execute(
                """INSERT INTO idcp_file_versions
                   (tenant_id, api_key_id, session_id, canonical_path, version_id,
                    content, content_sha256, content_length, expires_at)
                   VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s,
                           now() + (%s * interval '1 second'))""",
                (scope.tenant_id, scope.api_key_id, scope.session_id, canonical,
                 version.version_id, version.content, version.content_sha256,
                 len(version.content), self.ttl_seconds),
            )
        return result

    def _latest(self, scope: FileScope, canonical_path: str, *, lock: bool) -> FileVersion | None:
        sql = """SELECT canonical_path, version_id, content, content_sha256
                 FROM idcp_file_versions
                 WHERE tenant_id = %s::uuid AND api_key_id = %s::uuid
                   AND session_id = %s AND canonical_path = %s
                   AND expires_at > now()
                 ORDER BY created_at DESC, id DESC LIMIT 1"""
        if lock:
            sql += " FOR UPDATE"
        row = self.connection.execute(
            sql, (scope.tenant_id, scope.api_key_id, scope.session_id, canonical_path)
        ).fetchone()
        if row is None:
            return None
        return FileVersion(row[0], row[1], bytes(row[2]), row[3])
