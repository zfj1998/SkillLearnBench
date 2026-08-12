#!/usr/bin/env python3
"""Re-run the self-generated-skill session continuity audit from artifacts."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASKS_ROOT = ROOT / "tasks"
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

    heldouts = source.get("heldouts")
    if heldouts is not None:
        family = source.get("family_id")
        expected = source.get("heldout_expected")
        if not isinstance(expected, list) or not expected:
            raise RuntimeError("Runtime held-out expectation is missing")
        expected_numbers = []
        for instance_id in expected:
            match = re.fullmatch(rf"{re.escape(str(family))}-(\d+)", str(instance_id))
            if not match or int(match.group(1)) == 1:
                raise RuntimeError(f"Invalid held-out instance identity: {instance_id}")
            expected_numbers.append(int(match.group(1)))
        if expected_numbers != sorted(set(expected_numbers)):
            raise RuntimeError("Runtime held-out expectation is not unique numeric order")
        task_path = TASKS_ROOT / str(family) / f"{family}-1"
        repository_numbers = METHOD._family_heldout_numbers(task_path)
        repository_expected = [f"{family}-{number}" for number in repository_numbers]
        if expected != repository_expected:
            raise RuntimeError(
                "Runtime held-out expectation does not match the benchmark family: "
                f"expected {repository_expected}, saw {expected}"
            )
        if not isinstance(heldouts, list) or [x.get("instance_id") for x in heldouts] != expected:
            raise RuntimeError("Held-out artifact coverage or order mismatch")
        sessions = [x.get("session_id") for x in heldouts]
        if any(not isinstance(x, str) or not x for x in sessions):
            raise RuntimeError("Held-out session identity is missing")
        if len(set(sessions)) != len(expected) or session_id in sessions:
            raise RuntimeError("Held-out sessions are not distinct fresh sessions")
        if source.get("frozen_library_unchanged") is not True:
            raise RuntimeError("Frozen library mutation proof is absent")
        frozen_hashes = source.get("frozen_skill_sha256")
        if not isinstance(frozen_hashes, dict):
            raise RuntimeError("Frozen library manifest is malformed")
        actual_frozen_hashes = METHOD._file_hashes(trial / "frozen-skills")
        if actual_frozen_hashes != frozen_hashes:
            raise RuntimeError("Frozen library files disagree with the runtime manifest")
        if any(x.get("frozen_skill_sha256") != frozen_hashes for x in heldouts):
            raise RuntimeError("Held-out frozen library manifests disagree")

        pass_count = 0
        tests_path_mentions = 0
        for number, item in zip(expected_numbers, heldouts, strict=True):
            if item.get("hidden_tests_mounted_in_agent") is not False:
                raise RuntimeError(f"Hidden test isolation proof is absent for held-out {number}")
            verifier = (
                trial / f"heldout-instance-{number}" / "same-session-attempts"
                / "attempt-01" / "verifier"
            )
            reward = (verifier / "reward.txt").read_text(encoding="utf-8").strip()
            evidence_passed = reward == "1"
            if item.get("verifier_passed") is not evidence_passed:
                raise RuntimeError(f"Held-out {number} verifier report disagrees with reward.txt")
            if not (verifier / "ctrf.json").is_file():
                raise RuntimeError(f"Held-out {number} CTRF evidence is missing")
            if evidence_passed:
                pass_count += 1
            agent_text = (trial / f"heldout-instance-{number}" / "agent.jsonl").read_text(
                encoding="utf-8"
            )
            tests_path_mentions += agent_text.count("/tests")
        if tests_path_mentions:
            raise RuntimeError("Held-out agent trajectory mentions the hidden /tests path")
        score = pass_count / len(expected)
        if (
            source.get("heldout_pass_count") != pass_count
            or source.get("heldout_total") != len(expected)
            or source.get("heldout_score") != score
        ):
            raise RuntimeError("Held-out aggregate disagrees with verifier evidence")
        if source.get("skill_generation_valid") is not (source.get("skill_generation_status") == "valid"):
            raise RuntimeError("Skill-generation validity and status disagree")
        candidate_validation = json.loads(
            (trial / "skill-candidate-validation.json").read_text(encoding="utf-8")
        )
        for key in ("valid", "status", "skills", "candidate_skills", "validation_errors"):
            source_key = {
                "valid": "skill_generation_valid",
                "status": "skill_generation_status",
                "validation_errors": "skill_validation_errors",
            }.get(key, key)
            if candidate_validation.get(key) != source.get(source_key):
                raise RuntimeError(f"Skill candidate validation disagrees for {key}")
        if source.get("skill_generation_valid") is False and frozen_hashes:
            raise RuntimeError("Invalid skill candidate leaked into the frozen library")
        result.update({
            "family_id": family,
            "heldout_instances_verified": expected,
            "heldout_fresh_sessions_verified": True,
            "frozen_library_unchanged_verified": True,
            "heldout_hidden_tests_isolated": True,
            "heldout_pass_count": pass_count,
            "heldout_total": len(expected),
            "heldout_score": score,
            "skill_generation_valid": source.get("skill_generation_valid"),
            "skill_generation_status": source.get("skill_generation_status"),
        })
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
