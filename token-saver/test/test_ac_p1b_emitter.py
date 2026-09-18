"""AC-P1b supersession emitter unit tests (offline, tmp dirs).

The live artifact-state assertions are QA's (test_ac_p1b_supersession.py,
currently strict=False xfail until their live-run verification); these
tests pin the EMITTER's own contract: on every new artifact the previous
authoritative file is stamped `superseded_by: <new>` and older
generations move to results/archive/.
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


def test_stamps_previous_authoritative_with_new_name(tmp_path):
    _write(tmp_path, "benchmark_old_001.json")
    _write(tmp_path, "benchmark_new_002.json")
    out = stamp_supersession(tmp_path, "benchmark_new_002.json")
    assert out == {"supersedes": ["benchmark_old_001.json"], "archived": []}
    old = json.loads((tmp_path / "benchmark_old_001.json").read_text())
    assert old["superseded_by"] == "benchmark_new_002.json"
    new = json.loads((tmp_path / "benchmark_new_002.json").read_text())
    assert new["superseded_by"] is None  # new artifact untouched


def test_archives_older_generations_out_of_the_live_pair(tmp_path):
    _write(tmp_path, "benchmark_gen1_001.json", superseded_by="g2.json")
    _write(tmp_path, "benchmark_gen2_002.json", superseded_by="g3.json")
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


def test_authoritative_names_reads_pointer_state(tmp_path):
    _write(tmp_path, "benchmark_a_001.json")
    _write(tmp_path, "benchmark_b_002.json", superseded_by="x.json")
    assert _artifact_authoritative_names(tmp_path) == ["benchmark_a_001.json"]


def test_new_name_never_stamped_even_if_null(tmp_path):
    _write(tmp_path, "benchmark_new_009.json")  # written before stamp runs
    out = stamp_supersession(tmp_path, "benchmark_new_009.json")
    assert out == {"supersedes": [], "archived": []}
    data = json.loads((tmp_path / "benchmark_new_009.json").read_text())
    assert data["superseded_by"] is None
