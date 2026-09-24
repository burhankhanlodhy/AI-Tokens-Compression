"""Provisional synthetic IDCP replay; not representative task-quality evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from proxy.counting import count_text
from proxy.idcp import FileScope, prepare_file_read


def _session_text(session: int, revision: int) -> bytes:
    lines = [
        f"# Synthetic module {session:03d}; generated deterministic replay input\n",
        "def process_records(records):\n",
        "    results = []\n",
    ]
    lines.extend(f"    # stable context line {i:03d} for session {session:03d}\n" for i in range(72))
    lines.extend([
        f"    mode = 'mode-{revision}'\n",
        f"    limit = {100 + revision}\n",
        "    for record in records:\n",
        "        results.append((record, mode, limit))\n",
        "    return results\n",
    ])
    return "".join(lines).encode("utf-8")


def run_replay(*, session_count: int = 50, model: str = "gpt-4o") -> dict[str, Any]:
    if session_count < 1:
        raise ValueError("session_count must be positive")

    baseline_tokens = 0
    idcp_tokens = 0
    edit_count = 0
    silent_stale_applications = 0
    for session_index in range(session_count):
        scope = FileScope(f"tenant-{session_index}", f"key-{session_index}", f"session-{session_index}")
        path = f"src/module_{session_index:03d}.py"
        version = prepare_file_read(
            scope=scope, path=path, content=_session_text(session_index, 0), current=None
        ).new_version
        assert version is not None
        for revision in range(1, 4):
            next_content = _session_text(session_index, revision)
            record = prepare_file_read(
                scope=scope,
                path=path,
                content=next_content,
                current=version,
                expected_base_version_id=version.version_id,
            )
            baseline_tokens += count_text(next_content.decode(), model)
            representation = record.diff if record.kind == "diff" else record.content
            if representation is None:
                raise AssertionError(f"unexpected {record.kind} replay result")
            idcp_tokens += count_text(
                representation.decode() if isinstance(representation, bytes) else representation,
                model,
            )
            if record.kind == "diff" and record.base_version_id != version.version_id:
                silent_stale_applications += 1
            version = record.new_version
            assert version is not None
            edit_count += 1

    reduction_pct = (baseline_tokens - idcp_tokens) * 100 / baseline_tokens if baseline_tokens else 0.0
    return {
        "evidence_kind": "provisional_synthetic",
        "corpus_description": "Deterministically generated source-like files with three localized edits per session; no real user/task traces.",
        "representative_corpus": False,
        "sessions": session_count,
        "sessions_with_multiple_edits": session_count if edit_count >= session_count * 2 else 0,
        "edits": edit_count,
        "model_tokenizer": model,
        "baseline_repeated_file_tokens": baseline_tokens,
        "idcp_repeated_file_tokens": idcp_tokens,
        "repeated_file_context_reduction_pct": round(reduction_pct, 4),
        "task_success_regression_pp": None,
        "task_success_status": "not_measured_no_task_outcomes",
        "silent_stale_applications": silent_stale_applications,
        "gate_status": "NOT_EVALUABLE_SYNTHETIC_CORPUS_AND_NO_TASK_OUTCOMES",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=50)
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_replay(session_count=args.sessions, model=args.model)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
