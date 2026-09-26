"""Validate the Release-A fixed task suite (docs/v2.3-savings-validation-plan.md §6).

For every task, in a throwaway git worktree at the suite's base_commit:
  1. baseline: `guard` passes before any change;
  2. setup applies cleanly (each `old` string occurs exactly once);
  3. hidden tests are written, and `check` FAILS (the task is a real task);
  4. the reference fix applies, and `check` + `guard` PASS (the task is solvable).

A task that fails any step is not admissible for the A/B. Exits non-zero on
any failure. Uses the token-saver .venv from the main checkout.

Usage:  .venv/bin/python benchmark/task_suite/validate_suite.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_SAVER = HERE.parent.parent
REPO = TOKEN_SAVER.parent
VENV = TOKEN_SAVER / ".venv"


def sh(cmd: str, cwd: Path) -> int:
    return subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode


def apply_edits(root: Path, edits: list[dict], reverse: bool = False) -> None:
    for e in edits:
        old, new = (e["new"], e["old"]) if reverse else (e["old"], e["new"])
        p = root / e["file"]
        text = p.read_text()
        n = text.count(old)
        if n != 1:
            raise RuntimeError(f"{e['file']}: expected 1 match, found {n}")
        p.write_text(text.replace(old, new))


def validate(task: dict, guard: list[str], base: str) -> list[str]:
    errs: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix=f"suite-{task['id']}-"))
    wt = tmp / "wt"
    try:
        subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(wt), base],
                       check=True, capture_output=True)
        ts = wt / "token-saver"
        (ts / ".venv").symlink_to(VENV)
        if any(sh(c, ts) for c in guard):
            return ["guard fails at base_commit"]
        apply_edits(ts, task.get("setup", []))
        ht = task.get("hidden_test")
        if ht:
            (ts / ht["path"]).write_text(ht["content"])
        if all(sh(c, ts) == 0 for c in task["check"]):
            errs.append("check already passes after setup (not a real task)")
        fix = task["reference_fix"]
        if fix == "revert_setup":
            apply_edits(ts, task["setup"], reverse=True)
        else:
            apply_edits(ts, fix)
        if any(sh(c, ts) for c in task["check"]):
            errs.append("check fails after reference fix (not solvable)")
        if any(sh(c, ts) for c in guard):
            errs.append("guard fails after reference fix")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"harness error: {exc}")
    finally:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                       capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return errs


def main() -> int:
    suite = json.loads((HERE / "tasks.json").read_text())
    base = suite["_meta"]["base_commit"]
    bad = 0
    for task in suite["tasks"]:
        errs = validate(task, suite["guard"], base)
        print(f"{'PASS' if not errs else 'FAIL'}  {task['id']}" + "".join(f"\n      - {e}" for e in errs))
        bad += bool(errs)
    print(f"\n{len(suite['tasks']) - bad}/{len(suite['tasks'])} tasks admissible")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
