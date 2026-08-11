import importlib.util
import json
from pathlib import Path

import pytest


METHOD_PATH = (
    Path(__file__).parents[1]
    / "baselines/in-session-3try-skill-creator/method.py"
)
SPEC = importlib.util.spec_from_file_location("selfgen_method", METHOD_PATH)
assert SPEC and SPEC.loader
METHOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METHOD)


SESSION = "11111111-1111-4111-8111-111111111111"


def _record(kind, uuid, parent, text=None, content=None):
    if content is None:
        content = text if text is not None else []
    return {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SESSION,
        "isSidechain": False,
        "message": {"role": kind, "content": content},
    }


def _write(path, records):
    path.write_text("".join(json.dumps(item) + "\n" for item in records))


def _leaf(uuid):
    return {"type": "last-prompt", "sessionId": SESSION, "leafUuid": uuid}


def test_accepts_strict_prefix_parent_and_tool_links(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    records = [
        _record("user", "u1", None, text="solve"),
        _record("assistant", "a1", "u1", content=[{"type": "tool_use", "id": "t1"}]),
        _record("user", "u2", "a1", content=[{"type": "tool_result", "tool_use_id": "t1"}]),
        _record("assistant", "a2", "u2", text="done"),
        _leaf("a2"),
    ]
    _write(first, records)
    _write(second, records + [
        _record("user", "u3", "a2", text="reflect"),
        _record("assistant", "a3", "u3", text="captured"),
        _leaf("a3"),
    ])
    result = METHOD._audit_session_snapshot(
        second, session_id=SESSION, expected_prompt="reflect", previous_path=first
    )
    assert result["prefix_verified"] is True
    assert result["parent_links_verified"] is True
    assert result["tool_links_verified"] is True
    assert result["appended_records"] == 3


def test_rejects_non_prefix_snapshot(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write(first, [_record("user", "u1", None, text="solve"), _leaf("u1")])
    _write(second, [_record("user", "different", None, text="reflect"), _leaf("different")])
    with pytest.raises(RuntimeError, match="strict byte prefix"):
        METHOD._audit_session_snapshot(
            second, session_id=SESSION, expected_prompt="reflect", previous_path=first
        )


def test_accepts_parallel_tool_result_parent_graph(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    records = [
        _record("user", "u1", None, text="solve"),
        _record("assistant", "a1", "u1", content=[{"type": "tool_use", "id": "t1"}]),
        _record("assistant", "a2", "a1", content=[{"type": "tool_use", "id": "t2"}]),
        _record("user", "r1", "a1", content=[{"type": "tool_result", "tool_use_id": "t1"}]),
        _record("user", "r2", "a2", content=[{"type": "tool_result", "tool_use_id": "t2"}]),
        _record("assistant", "done", "r2", text="done"),
        _leaf("done"),
    ]
    _write(first, records)
    _write(second, records + [
        _record("user", "u2", "done", text="reflect"),
        _record("assistant", "a3", "u2", text="captured"),
        _leaf("a3"),
    ])
    result = METHOD._audit_session_snapshot(
        second, session_id=SESSION, expected_prompt="reflect", previous_path=first
    )
    assert result["parent_links_verified"] is True


def test_rejects_unresolved_tool_call(tmp_path):
    path = tmp_path / "session.jsonl"
    _write(path, [
        _record("user", "u1", None, text="solve"),
        _record("assistant", "a1", "u1", content=[{"type": "tool_use", "id": "t1"}]),
        _leaf("a1"),
    ])
    with pytest.raises(RuntimeError, match="unresolved tool calls"):
        METHOD._audit_session_snapshot(path, session_id=SESSION, expected_prompt="solve")


def test_rejects_missing_exact_appended_prompt(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    records = [_record("user", "u1", None, text="solve"), _leaf("u1")]
    _write(first, records)
    _write(second, records + [_record("assistant", "a1", "u1", text="reflect"), _leaf("a1")])
    with pytest.raises(RuntimeError, match="exactly one appended user prompt"):
        METHOD._audit_session_snapshot(
            second, session_id=SESSION, expected_prompt="reflect", previous_path=first
        )


def test_skill_candidate_classification_keeps_invalid_output_as_model_behavior():
    empty = METHOD._classify_skill_candidate({})
    assert empty["valid"] is False
    assert empty["status"] == "no_skill_created"
    assert empty["skills"] == []

    invalid = METHOD._classify_skill_candidate({"/root/skills/bad/SKILL.md": "no frontmatter"})
    assert invalid["valid"] is False
    assert invalid["status"] == "invalid_skill_candidate"
    assert invalid["candidate_skills"] == ["/root/skills/bad/SKILL.md"]
    assert invalid["skills"] == []

    valid = METHOD._classify_skill_candidate({
        "/root/skills/good/SKILL.md": "---\nname: good\ndescription: reusable guidance\n---\n"
    })
    assert valid["valid"] is True
    assert valid["status"] == "valid"
    assert valid["skills"] == ["/root/skills/good/SKILL.md"]


def test_evaluate_heldouts_covers_instances_two_through_five(monkeypatch, tmp_path):
    seen = []

    def fake_heldout_eval(**kwargs):
        seen.append(kwargs["instance_number"])
        return {"instance_id": f"family-{kwargs['instance_number']}"}

    monkeypatch.setattr(METHOD, "_heldout_eval", fake_heldout_eval)
    results = METHOD._evaluate_heldouts(
        task_path=tmp_path / "family/family-1",
        trial_path=tmp_path,
        frozen=tmp_path / "frozen",
        agent={},
        model_name="model",
        max_steps=1,
    )
    assert seen == [2, 3, 4, 5]
    assert [item["instance_id"] for item in results] == [
        "family-2", "family-3", "family-4", "family-5"
    ]
