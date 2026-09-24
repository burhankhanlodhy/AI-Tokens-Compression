"""Keep release metadata and user-facing configuration docs aligned."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "token-saver"))

from proxy.version import __version__  # noqa: E402


def test_release_version_and_changelog_are_consistent():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert __version__ == "2.0.0"
    assert "## [1.2.2] - Unreleased" in changelog
    assert f"## [{__version__}] - 2026-09-24" in changelog


def test_codebase_settings_are_discoverable_in_all_user_docs():
    settings = {
        "CODEBASE_OPTIMIZATION_ENABLED",
        "CODEBASE_MAX_FILE_LINES",
        "CODEBASE_DEDUPE_IMPORTS",
        "SHELL_OUTPUT_FILTERING",
    }
    docs = {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        ".env.example": (ROOT / "token-saver" / ".env.example").read_text(encoding="utf-8"),
        "TUNING.md": (ROOT / "TUNING.md").read_text(encoding="utf-8"),
    }

    for path, content in docs.items():
        missing = {name for name in settings if name not in content}
        assert not missing, f"{path} is missing settings: {sorted(missing)}"


def test_codebase_tuning_has_v121_context_and_measured_corpus_result():
    tuning = re.sub(r"\s+", " ", (ROOT / "TUNING.md").read_text(encoding="utf-8"))

    assert "Codebase-context optimization (v1.2.1 candidate)" in tuning
    assert "92.61% as-shipped codebase-segment input-token reduction" in tuning
    assert "result is population-specific, not a per-request guarantee" in tuning
    assert "af21ac53e8d1da7f2ab4402a0573ec3709bc5d92513a3ecbc8ce575f6f10a33a" in tuning


def test_v121_measurements_follow_ratified_publication_contract():
    changelog = re.sub(r"\s+", " ", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    tuning = re.sub(r"\s+", " ", (ROOT / "TUNING.md").read_text(encoding="utf-8"))

    for content in (changelog, tuning):
        assert "NO-GO" in content
        assert "15.51% isolated transformer contribution" in content
        assert "0.55% as-shipped marginal" in content
        assert "e689f2c7fc8accf6140f2b22dc19bceb0b3e4012d14c0517cb947d40d97d2284" in content
        assert "1.22%" in content
        assert "43.9%" in content
        assert "100% (40/40 eligible turns)" in content
        assert "0.00%" in content
