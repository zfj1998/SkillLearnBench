#!/usr/bin/env python3
"""Re-run the self-generated-skill session continuity audit from artifacts."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METHOD_PATH = ROOT / "baselines/in-session-3try-skill-creator/method.py"
SPEC = importlib.util.spec_from_file_location("selfgen_method", METHOD_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load selfgen method: {METHOD_PATH}")
METHOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METHOD)


def audit_trial(trial: Path) -> dict:
    source = json.loads((trial / "selfgen_audit.json").read_text(encoding="utf-8"))
    session_id = source["session_id"]
    attempts = int(source["attempts_used"])
    snapshots = []
    previous = None
    for attempt in range(1, attempts + 1):
        phase = trial / "same-session-attempts" / f"attempt-{attempt:02d}"
        snapshot = phase / "claude-session.jsonl"
        prompt = (phase / "phase-prompt.txt").read_text(encoding="utf-8")
        snapshots.append(METHOD._audit_session_snapshot(
            snapshot, session_id=session_id, expected_prompt=prompt,
            previous_path=previous,
        ))
        previous = snapshot
    reflection_snapshot = trial / "reflection-session.jsonl"
    reflection_prompt = (trial / "reflection-prompt.txt").read_text(encoding="utf-8")
    snapshots.append(METHOD._audit_session_snapshot(
        reflection_snapshot, session_id=session_id,
        expected_prompt=reflection_prompt, previous_path=previous,
    ))

    result = {
        "protocol": source["protocol"],
        "scoreable": False,
        "session_id": session_id,
        "attempts_used": attempts,
        "session_snapshot_count": len(snapshots),
        "parent_continuity_verified": all(x["parent_links_verified"] for x in snapshots),
        "prefix_continuity_verified": all(x["prefix_verified"] for x in snapshots[1:]),
        "prompt_continuity_verified": all(x["prompt_verified"] for x in snapshots),
        "tool_link_continuity_verified": all(x["tool_links_verified"] for x in snapshots),
        "snapshots": snapshots,
    }
    for key in (
        "parent_continuity_verified", "prefix_continuity_verified",
        "prompt_continuity_verified", "tool_link_continuity_verified",
    ):
        if source.get(key) is not result[key]:
            raise RuntimeError(f"Runtime and offline continuity results disagree for {key}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trial", type=Path, help="Directory containing selfgen_audit.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_trial(args.trial.resolve())
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
