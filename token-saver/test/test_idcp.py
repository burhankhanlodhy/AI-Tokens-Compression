from proxy.idcp import FileScope, IDCPStore, prepare_file_read
from proxy.counting import count_text


def test_first_read_returns_full_content_and_immutable_version():
    result = prepare_file_read(
        scope=FileScope("tenant-a", "key-a", "session-a"),
        path="src/../src/main.py",
        content=b"print('one')\n",
        current=None,
    )

    assert result.kind == "full"
    assert result.canonical_path == "src/main.py"
    assert result.content == b"print('one')\n"
    assert result.version_id
    assert result.base_version_id is None


def test_unchanged_read_is_explicit_and_reuses_version():
    scope = FileScope("tenant-a", "key-a", "session-a")
    first = prepare_file_read(scope=scope, path="a.py", content=b"x\n", current=None)
    same = prepare_file_read(
        scope=scope, path="a.py", content=b"x\n", current=first.new_version
    )

    assert same.kind == "unchanged"
    assert same.version_id == first.version_id
    assert same.content is None


def test_small_change_returns_reconstructable_versioned_unified_diff():
    scope = FileScope("tenant-a", "key-a", "session-a")
    old_content = ("line %02d: unchanged source content\n" % i for i in range(20))
    old_content = "".join(old_content).encode()
    new_content = old_content.replace(b"line 10: unchanged", b"line 10: edited")
    first = prepare_file_read(scope=scope, path="a.py", content=old_content, current=None)
    changed = prepare_file_read(
        scope=scope, path="a.py", content=new_content, current=first.new_version
    )

    assert changed.kind == "diff"
    assert changed.canonical_path == "a.py"
    assert changed.base_version_id == first.version_id
    assert changed.version_id != first.version_id
    assert "--- a/a.py" in changed.diff
    assert "+++ b/a.py" in changed.diff
    assert changed.new_version.content == new_content
    old_lines = old_content.decode().splitlines(keepends=True)
    patch_lines = changed.diff.splitlines(keepends=True)
    hunk = next(line for line in patch_lines if line.startswith("@@ "))
    old_start = int(hunk.split(" ")[1].split(",")[0][1:]) - 1
    old_count = int(hunk.split(" ")[1].split(",")[1])
    reconstructed = old_lines[:old_start]
    for line in patch_lines[patch_lines.index(hunk) + 1:]:
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            reconstructed.append(line[1:])
        elif line.startswith(" "):
            reconstructed.append(line[1:])
    reconstructed.extend(old_lines[old_start + old_count:])
    assert "".join(reconstructed).encode() == changed.new_version.content


def test_large_diff_falls_back_to_full_content():
    scope = FileScope("tenant-a", "key-a", "session-a")
    old = b"old line\n" * 100
    new = b"entirely changed line\n" * 100
    first = prepare_file_read(scope=scope, path="a.txt", content=old, current=None)
    changed = prepare_file_read(
        scope=scope, path="a.txt", content=new, current=first.new_version,
        max_diff_bytes=20,
    )

    assert changed.kind == "full"
    assert changed.diff is None
    assert changed.content == new


def test_deletion_and_rename_ambiguity_fall_back_without_silent_patch():
    scope = FileScope("tenant-a", "key-a", "session-a")
    first = prepare_file_read(scope=scope, path="old.py", content=b"x\n", current=None)
    deleted = prepare_file_read(
        scope=scope, path="old.py", content=None, current=first.new_version
    )
    renamed = prepare_file_read(
        scope=scope, path="new.py", content=b"x\n", current=None
    )

    assert deleted.kind == "deleted"
    assert deleted.diff is None
    assert renamed.kind == "full"


def test_stale_base_concurrent_change_and_full_context_request_fall_back():
    scope = FileScope("tenant-a", "key-a", "session-a")
    first = prepare_file_read(scope=scope, path="a.py", content=b"x\n", current=None)
    latest = prepare_file_read(
        scope=scope, path="a.py", content=b"y\n", current=first.new_version
    )
    stale = prepare_file_read(
        scope=scope, path="a.py", content=b"z\n", current=latest.new_version,
        expected_base_version_id=first.version_id,
    )
    concurrent = prepare_file_read(
        scope=scope, path="a.py", content=b"z\n", current=latest.new_version,
        expected_base_version_id=first.version_id,
    )
    full_requested = prepare_file_read(
        scope=scope, path="a.py", content=b"y\n", current=latest.new_version,
        full_context=True,
    )

    assert stale.kind == concurrent.kind == "full"
    assert stale.reason == concurrent.reason == "stale_base"
    assert stale.content == b"z\n"
    assert full_requested.kind == "full"
    assert full_requested.reason == "full_context_requested"


def test_ambiguous_utf8_or_validation_risk_falls_back_to_full():
    scope = FileScope("tenant-a", "key-a", "session-a")
    before = b"stable line\n" * 20
    first = prepare_file_read(scope=scope, path="a.py", content=before, current=None)
    binary = prepare_file_read(
        scope=scope, path="a.py", content=b"\xff\x00" * 30, current=first.new_version
    )
    invalid = prepare_file_read(
        scope=scope, path="a.py", content=b"changed line\n" + before[12:], current=first.new_version,
        validate_diff=lambda _diff: False,
    )

    assert binary.kind == invalid.kind == "full"
    assert binary.reason == "ambiguous_content"
    assert invalid.reason == "validation_failed"


def test_canonical_paths_reject_traversal_and_scope_is_required():
    from proxy.idcp import canonicalize_path

    assert canonicalize_path("/repo/src/../main.py") == "/repo/main.py"
    for bad_path in ("../../secret", "", "."):
        try:
            canonicalize_path(bad_path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe path {bad_path!r}")

    try:
        FileScope("tenant-a", "", "session-a")
    except ValueError:
        pass
    else:
        raise AssertionError("accepted missing API-key scope")


def test_offline_token_accounting_counts_full_vs_repeated_diff_context():
    scope = FileScope("tenant-a", "key-a", "session-a")
    before = "def calculate_total(items):\n" + "    return sum(items)\n" * 40
    after = "def calculate_total(items):\n" + "    return sum(item * 2 for item in items)\n" + "    return sum(items)\n" * 39
    first = prepare_file_read(scope=scope, path="src/calc.py", content=before.encode(), current=None)
    changed = prepare_file_read(scope=scope, path="src/calc.py", content=after.encode(), current=first.new_version)

    baseline_repeated_tokens = count_text(after, "gpt-4o")
    treatment_repeated_tokens = count_text(changed.diff, "gpt-4o")
    assert changed.kind == "diff"
    assert treatment_repeated_tokens < baseline_repeated_tokens


class _Rows:
    def fetchone(self):
        return None


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=()):
        self.calls.append((query, params))
        return _Rows()


def test_store_is_default_off_and_enabled_reads_use_scoped_append_only_ledger():
    connection = _Connection()
    store = IDCPStore(connection)
    scope = FileScope("tenant-a", "key-a", "session-a")

    disabled = store.read(scope, "a.py", b"code\n")
    assert disabled.kind == "full"
    assert connection.calls == []

    enabled = store.read(scope, "a.py", b"code\n", enabled=True)
    assert enabled.kind == "full"
    assert len(connection.calls) == 3  # advisory lock, scoped read, immutable insert
    lock_sql, _ = connection.calls[0]
    read_sql, read_params = connection.calls[1]
    insert_sql, insert_params = connection.calls[2]
    assert "pg_advisory_xact_lock" in lock_sql
    assert "tenant_id = %s::uuid AND api_key_id = %s::uuid" in read_sql
    assert "session_id = %s AND canonical_path = %s" in read_sql
    assert read_params == ("tenant-a", "key-a", "session-a", "a.py")
    assert "INSERT INTO idcp_file_versions" in insert_sql
    assert insert_params[:5] == ("tenant-a", "key-a", "session-a", "a.py", enabled.version_id)
