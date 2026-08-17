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

_HIDDEN_TESTS_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/tests(?:/|\b)")


def _hidden_tests_path_mentions(agent_jsonl: Path) -> int:
    """Count model-authored references to the hidden verifier mount.

    Inspect assistant events rather than the raw JSONL so a benchmark prompt
    cannot indict itself. The left boundary also avoids treating ordinary
    prose such as ``compilation/tests`` as an absolute ``/tests`` path.
    """
    mentions = 0
    for line_number, line in enumerate(
        agent_jsonl.read_text(encoding="utf-8").splitlines(), start=1
    ):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Malformed held-out agent JSONL at {agent_jsonl}:{line_number}"
            ) from exc
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        authored = json.dumps(message, ensure_ascii=False) if isinstance(message, dict) else ""
        mentions += len(_HIDDEN_TESTS_PATH.findall(authored))
    return mentions


def _validate_verifier_evidence(verifier: Path, heldout_task: Path) -> dict:
    """Validate the official evidence format actually emitted by this task.

    Most SkillLearnBench tasks emit pytest CTRF, but a small source-defined
    subset uses another official runner and writes its own log beside
    reward.txt. Requiring CTRF for those tasks rejects valid benchmark evidence;
    accepting only reward.txt would be too weak.
    """
    ctrf = verifier / "ctrf.json"
    if ctrf.is_file():
        try:
            payload = json.loads(ctrf.read_text(encoding="utf-8"))
            results = payload["results"]
            if not isinstance(results, dict) or not isinstance(results.get("summary"), dict):
                raise TypeError("results.summary is not an object")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Malformed CTRF evidence at {ctrf}: {exc}") from exc
        return {"kind": "ctrf", "files": ["ctrf.json"]}

    test_script = heldout_task / "tests" / "test.sh"
    script_text = test_script.read_text(encoding="utf-8")
    if "--ctrf" in script_text or "pytest-json-ctrf" in script_text:
        raise RuntimeError(f"CTRF evidence required by {test_script} is missing")

    wrapper_files = {"reward.txt", "stdout.txt", "stderr.txt"}
    native_logs = sorted(
        path for path in verifier.iterdir()
        if path.is_file() and path.name not in wrapper_files and path.stat().st_size > 0
    )
    if not native_logs:
        raise RuntimeError(
            f"Non-CTRF verifier evidence is missing for {heldout_task.name}"
        )
    return {
        "kind": "native-log",
        "files": [path.name for path in native_logs],
    }


def audit_trial(trial: Path) -> dict:
    source = json.loads((trial / "selfgen_audit.json").read_text(encoding="utf-8"))
    scoreable = source.get("scoreable")
    if type(scoreable) is not bool:
        raise RuntimeError("Runtime scoreable marker is missing or malformed")
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
        "scoreable": scoreable,
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
        verifier_evidence = []
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
            heldout_task = TASKS_ROOT / str(family) / f"{family}-{number}"
            evidence = _validate_verifier_evidence(verifier, heldout_task)
            verifier_evidence.append({"instance_id": item["instance_id"], **evidence})
            if evidence_passed:
                pass_count += 1
            tests_path_mentions += _hidden_tests_path_mentions(
                trial / f"heldout-instance-{number}" / "agent.jsonl"
            )
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
            "heldout_verifier_evidence": verifier_evidence,
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
