"""AC-P1e/P1f: published L1 production-path benchmark contract."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_production_path_emits_named_published_arms(tmp_path):
    """The committed benchmark names both published arms and proves no drift.

    The production-default C1+C2+C3 arm is the marketing claim; the C1-only
    arm is the conservative comparison.  Both values must come from the same
    checksum-pinned run, whose end-to-end production path equals the cleaner
    measurement rather than merely looking healthy in isolation.
    """
    subprocess.run(
        [
            sys.executable,
            "benchmark/run_l1_benchmark.py",
            "--production-path",
            "--out",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads((tmp_path / "l1_b2_results.json").read_text())

    assert result["schema"] == "l1_b2_v2"
    assert result["taxonomy_version"] == "1.2"
    assert result["production_path"]["ordering"] == (
        "classify(raw) -> l1_eligible(messages, route) -> clean_messages"
    )

    published = result["published_savings"]
    assert published["production_default"]["variant"] == "C1+C2+C3"
    assert published["conservative_c1_only"]["variant"] == "C1"
    assert set(published["production_default"]["per_content_class"]) == {
        "rag", "json_doc", "system_dup", "log_trace"
    }
    item_range = published["production_default"]["item_reduction_range_pct"]
    assert item_range["min_pct"] <= item_range["median_pct"] <= item_range["max_pct"]
    assert (
        published["production_default"]["reduction_pct"]
        == result["production_path"]["strippable4"]["reduction_pct"]
        == result["decomposition"]["full"]["strippable4"]["reduction_pct"]
    )
    assert (
        published["conservative_c1_only"]["reduction_pct"]
        == result["decomposition"]["c1_only"]["strippable4"]["reduction_pct"]
    )
    assert result["ac_p1e_pass"] is True
    assert result["violations"] == []
