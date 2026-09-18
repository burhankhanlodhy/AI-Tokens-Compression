"""AC-P1b extension (P1 artifact supersession, PM 2026-09-18).

Spec (product-spec-v2.md, "P1 artifact supersession"): every committed
benchmark artifact must carry a `superseded_by` field naming the artifact
that replaces it; the authoritative artifact carries `superseded_by: null`.
No consumer should need commit messages to know which of two artifacts in
`benchmark/results/` is authoritative.

QA asserts the field on any artifact pair found in the results directory:

1. every artifact in `benchmark/results/benchmark_*.json` carries the
   `superseded_by` key (None on the authoritative one, a filename string
   on a superseded one);
2. `run_benchmark.py` writes the field on every new artifact;
3. when a superseded artifact names its replacement, that replacement
   file must actually exist and must NOT itself be superseded (no
   supersession chains / dangling pointers).

Status: emitter landed in b25fead/6a96eba; these assertions are now
release-blocking and must not silently xfail a supersession regression.
The temporary xfail markers are removed; each check is hard-required.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOKEN_SAVER = Path(__file__).resolve().parent.parent
RESULTS = TOKEN_SAVER / "benchmark" / "results"
RUNNER = TOKEN_SAVER / "benchmark" / "run_benchmark.py"

sys.path.insert(0, str(TOKEN_SAVER))
sys.path.insert(0, str(TOKEN_SAVER / "benchmark"))


def _benchmark_artifacts() -> list[Path]:
    if not RESULTS.is_dir():
        return []
    return sorted(RESULTS.glob("benchmark_*.json"))


def test_every_artifact_carries_superseded_by():
    artifacts = _benchmark_artifacts()
    assert artifacts, "no benchmark artifacts found in benchmark/results/"
    missing = []
    for path in artifacts:
        data = json.loads(path.read_text())
        if "superseded_by" not in data:
            missing.append(path.name)
    assert not missing, f"artifacts missing `superseded_by`: {missing}"


def test_superseded_by_pointers_resolve_and_terminate():
    pointers_seen = []
    for path in _benchmark_artifacts():
        data = json.loads(path.read_text())
        target = data.get("superseded_by")
        if target is None:
            continue
        pointers_seen.append((path.name, target))
        successor = RESULTS / target
        assert successor.is_file(), (
            f"{path.name} names nonexistent successor {target}"
        )
        successor_data = json.loads(successor.read_text())
        assert successor_data.get("superseded_by") is None, (
            f"supersession chain: {target} is itself superseded by "
            f"{successor_data.get('superseded_by')}"
        )
    # Vacuous-pass guard: an empty loop would otherwise exercise no
    # supersession pointer while still appearing green.
    assert pointers_seen, (
        "vacuous: no artifact in benchmark/results/ carries `superseded_by` "
        "— the pointer-resolution check exercised nothing"
    )


def test_at_most_one_authoritative_artifact():
    artifacts = _benchmark_artifacts()
    if not artifacts:
        pytest.skip("no artifacts")
    superseded = {
        json.loads(p.read_text()).get("superseded_by")
        for p in artifacts
    }
    superseded.discard(None)
    authoritative = [p.name for p in artifacts if p.name not in superseded]
    assert len(authoritative) <= 1, (
        f"multiple authoritative artifacts (none names them superseded): "
        f"{authoritative}"
    )


def test_runner_source_references_superseded_by():
    """The field must be written by the emitter, not just present in old
    files by hand — otherwise the next run silently drops it again."""
    source = RUNNER.read_text()
    assert "superseded_by" in source, (
        "run_benchmark.py never writes `superseded_by`; the next artifact "
        "will silently drop the supersession marker"
    )
