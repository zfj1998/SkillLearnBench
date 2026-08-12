import importlib.util
import json
from pathlib import Path


AUDITOR_PATH = Path(__file__).parents[1] / "ap/audit_selfgen_continuity.py"
SPEC = importlib.util.spec_from_file_location("selfgen_auditor", AUDITOR_PATH)
assert SPEC and SPEC.loader
AUDITOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDITOR)

SESSION = "11111111-1111-4111-8111-111111111111"


def _record(kind, uuid, parent, text):
    return {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SESSION,
        "isSidechain": False,
        "message": {"role": kind, "content": text},
    }


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_full_family_audit_accepts_invalid_skill_without_dropping_heldouts(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    family_root = tasks_root / "family"
    for number in range(1, 7):
        instance = family_root / f"family-{number}"
        instance.mkdir(parents=True)
        (instance / "instruction.md").write_text(f"instance {number}")
    monkeypatch.setattr(AUDITOR, "TASKS_ROOT", tasks_root)
    (tmp_path / "frozen-skills").mkdir()
    attempt = tmp_path / "same-session-attempts/attempt-01"
    attempt.mkdir(parents=True)
    solve = [
        _record("user", "u1", None, "solve"),
        _record("assistant", "a1", "u1", "done"),
        {"type": "last-prompt", "sessionId": SESSION, "leafUuid": "a1"},
    ]
    reflection = solve + [
        _record("user", "u2", "a1", "reflect"),
        _record("assistant", "a2", "u2", "captured nothing"),
        {"type": "last-prompt", "sessionId": SESSION, "leafUuid": "a2"},
    ]
    _write_jsonl(attempt / "claude-session.jsonl", solve)
    (attempt / "phase-prompt.txt").write_text("solve")
    _write_jsonl(tmp_path / "reflection-session.jsonl", reflection)
    (tmp_path / "reflection-prompt.txt").write_text("reflect")

    heldouts = []
    for number in range(2, 7):
        passed = number % 2 == 0
        root = tmp_path / f"heldout-instance-{number}"
        verifier = root / "same-session-attempts/attempt-01/verifier"
        verifier.mkdir(parents=True)
        (verifier / "reward.txt").write_text("1\n" if passed else "0\n")
        (verifier / "ctrf.json").write_text(json.dumps({"results": {"summary": {}}}))
        (root / "agent.jsonl").write_text("{}\n")
        heldouts.append({
            "instance_id": f"family-{number}",
            "session_id": f"22222222-2222-4222-8222-22222222222{number}",
            "verifier_passed": passed,
            "frozen_skill_sha256": {},
            "hidden_tests_mounted_in_agent": False,
        })

    source = {
        "protocol": "in-session-3try-skill-creator-family-v2",
        "scoreable": False,
        "family_id": "family",
        "session_id": SESSION,
        "attempts_used": 1,
        "parent_continuity_verified": True,
        "prefix_continuity_verified": True,
        "prompt_continuity_verified": True,
        "tool_link_continuity_verified": True,
        "skill_generation_valid": False,
        "skill_generation_status": "no_skill_created",
        "skills": [],
        "candidate_skills": [],
        "skill_validation_errors": ["Skill Creator produced no SKILL.md files"],
        "frozen_skill_sha256": {},
        "frozen_library_unchanged": True,
        "heldouts": heldouts,
        "heldout_expected": [f"family-{number}" for number in range(2, 7)],
        "heldout_pass_count": 3,
        "heldout_total": 5,
        "heldout_score": 0.6,
    }
    (tmp_path / "selfgen_audit.json").write_text(json.dumps(source))
    (tmp_path / "skill-candidate-validation.json").write_text(json.dumps({
        "valid": False,
        "status": "no_skill_created",
        "skills": [],
        "candidate_skills": [],
        "validation_errors": ["Skill Creator produced no SKILL.md files"],
    }))
    result = AUDITOR.audit_trial(tmp_path)
    assert result["skill_generation_valid"] is False
    assert result["heldout_instances_verified"] == [
        "family-2", "family-3", "family-4", "family-5", "family-6"
    ]
    assert result["heldout_pass_count"] == 3
    assert result["heldout_score"] == 0.6
