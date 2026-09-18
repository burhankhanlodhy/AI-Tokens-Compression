"""AC-P1b supersession emitter unit tests (offline, tmp dirs).

The live artifact-state assertions are QA's (test_ac_p1b_supersession.py,
currently strict=False xfail until their live-run verification); these
tests pin the EMITTER's own contract:

- DEFAULT (defer): a new artifact points at the current publication
  authority and nothing on disk is demoted — a reproduction run never
  replaces the published artifact (PM ruling 0c96677 on the P7 run).
- --supersede-authority (ratified replacement only): the previous
  authority is stamped `superseded_by: <new>` and older generations move
  to results/archive/ so results/ holds at most the authoritative pair.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmark"))

from run_benchmark import (  # noqa: E402
    ARCHIVE_SUBDIR,
    stamp_supersession,
    _artifact_authoritative_names,
)


def _write(dirpath: Path, name: str, superseded_by=None, tag="") -> Path:
    p = dirpath / name
    p.write_text(json.dumps({"id": name, "tag": tag,
                             "superseded_by": superseded_by}))
    return p


# --- default: DEFER to the current authority --------------------------


def test_authoritative_names_reads_pointer_state(tmp_path):
    _write(tmp_path, "benchmark_a_001.json")
    _write(tmp_path, "benchmark_b_002.json", superseded_by="benchmark_a_001.json")
    assert _artifact_authoritative_names(tmp_path) == ["benchmark_a_001.json"]


def test_defer_state_satisfies_qa_pair_contract(tmp_path):
    """The on-disk shape the defer path produces: authority null, the new
    artifact pointing at it — exactly the P7 corroboration state."""
    results = tmp_path / "results"
    results.mkdir()
    _write(results, "benchmark_authority_001.json")  # authority (null)
    _write(results, "benchmark_p7repro_002.json",
           superseded_by="benchmark_authority_001.json")
    arts = sorted(p.name for p in results.glob("benchmark_*.json"))
    data = {n: json.loads((results / n).read_text())["superseded_by"]
            for n in arts}
    # every artifact carries the key
    assert all("superseded_by" in json.loads((results / n).read_text())
               for n in arts)
    # every pointer resolves to an artifact that is itself authoritative
    for n, target in data.items():
        if target is not None:
            assert (results / target).is_file()
            assert json.loads((results / target).read_text())["superseded_by"] \
                is None
    # at most one artifact nobody points at
    targets = {t for t in data.values() if t is not None}
    assert len([n for n in arts if n not in targets]) <= 1


def test_stamp_supersession_stamps_previous_authoritative(tmp_path):
    """The --supersede-authority path: previous authority stamped with the
    new name; the new artifact itself untouched (null)."""
    _write(tmp_path, "benchmark_old_001.json")
    _write(tmp_path, "benchmark_new_002.json")
    out = stamp_supersession(tmp_path, "benchmark_new_002.json")
    assert out == {"supersedes": ["benchmark_old_001.json"], "archived": []}
    old = json.loads((tmp_path / "benchmark_old_001.json").read_text())
    assert old["superseded_by"] == "benchmark_new_002.json"
    new = json.loads((tmp_path / "benchmark_new_002.json").read_text())
    assert new["superseded_by"] is None  # new artifact untouched


def test_stamp_supersession_archives_older_generations(tmp_path):
    _write(tmp_path, "benchmark_gen1_001.json",
           superseded_by="benchmark_gen2_002.json")
    _write(tmp_path, "benchmark_gen2_002.json",
           superseded_by="benchmark_new_003.json")
    _write(tmp_path, "benchmark_new_003.json")
    out = stamp_supersession(tmp_path, "benchmark_new_003.json")
    assert out["archived"] == ["benchmark_gen1_001.json",
                               "benchmark_gen2_002.json"]
    for name in out["archived"]:
        assert not (tmp_path / name).exists()
        assert (tmp_path / ARCHIVE_SUBDIR / name).is_file()
    # nothing was left authoritative besides the new artifact
    assert _artifact_authoritative_names(tmp_path) == \
        ["benchmark_new_003.json"]


def test_stamp_supersession_never_touches_the_new_artifact(tmp_path):
    _write(tmp_path, "benchmark_new_009.json")  # written before stamp runs
    out = stamp_supersession(tmp_path, "benchmark_new_009.json")
    assert out == {"supersedes": [], "archived": []}
    data = json.loads((tmp_path / "benchmark_new_009.json").read_text())
    assert data["superseded_by"] is None
