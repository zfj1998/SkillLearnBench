import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

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


def test_accepts_last_prompt_marker_before_referenced_leaf(tmp_path):
    path = tmp_path / "session.jsonl"
    _write(path, [
        _record("user", "u1", None, text="solve"),
        _leaf("a1"),
        _record("assistant", "a1", "u1", text="done"),
    ])
    result = METHOD._audit_session_snapshot(
        path, session_id=SESSION, expected_prompt="solve"
    )
    assert result["leaf_uuid"] == "a1"


def test_rejects_last_prompt_leaf_missing_from_complete_snapshot(tmp_path):
    path = tmp_path / "session.jsonl"
    _write(path, [
        _record("user", "u1", None, text="solve"),
        _leaf("missing"),
    ])
    with pytest.raises(RuntimeError, match="Invalid Claude transcript last-prompt leaf"):
        METHOD._audit_session_snapshot(path, session_id=SESSION, expected_prompt="solve")


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


def test_accepts_compaction_boundary_with_verified_logical_parent(tmp_path):
    path = tmp_path / "session.jsonl"
    records = [
        _record("user", "u1", None, text="solve"),
        _record("assistant", "a1", "u1", text="working"),
        {
            "type": "system", "subtype": "compact_boundary", "uuid": "compact",
            "parentUuid": None, "logicalParentUuid": "a1", "sessionId": SESSION,
            "isSidechain": False,
            "compactMetadata": {"preservedSegment": {"tailUuid": "a1"}},
        },
        _record("assistant", "a2", "compact", text="done"),
        _leaf("a2"),
    ]
    _write(path, records)
    result = METHOD._audit_session_snapshot(path, session_id=SESSION, expected_prompt="solve")
    assert result["compact_boundaries"] == 1
    assert result["parent_links_verified"] is True


def test_rejects_compaction_boundary_without_verified_logical_parent(tmp_path):
    path = tmp_path / "session.jsonl"
    records = [
        _record("user", "u1", None, text="solve"),
        {
            "type": "system", "subtype": "compact_boundary", "uuid": "compact",
            "parentUuid": None, "logicalParentUuid": "missing", "sessionId": SESSION,
            "isSidechain": False,
            "compactMetadata": {"preservedSegment": {"tailUuid": "missing"}},
        },
        _leaf("u1"),
    ]
    _write(path, records)
    with pytest.raises(RuntimeError, match="Invalid Claude compact boundary link"):
        METHOD._audit_session_snapshot(path, session_id=SESSION, expected_prompt="solve")


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


def test_scoreable_mode_is_explicit_and_fail_closed(monkeypatch):
    monkeypatch.delenv("SELFGEN_SCOREABLE", raising=False)
    assert METHOD._scoreable_mode() is False
    monkeypatch.setenv("SELFGEN_SCOREABLE", "true")
    assert METHOD._scoreable_mode() is True
    monkeypatch.setenv("SELFGEN_SCOREABLE", "yes")
    with pytest.raises(RuntimeError, match="must be true or false"):
        METHOD._scoreable_mode()


def test_selfgen_timeouts_cover_long_opus_turns():
    assert METHOD._CLAUDE_TURN_TIMEOUT_SECONDS == 7200
    assert METHOD._HELDOUT_CONTAINER_KEEPALIVE_SECONDS > METHOD._CLAUDE_TURN_TIMEOUT_SECONDS


def test_binary_safe_cat_preserves_text_and_redacts_binary(tmp_path):
    wrapper = tmp_path / "cat"
    wrapper.write_text(METHOD._BINARY_SAFE_CAT)
    wrapper.chmod(0o755)
    text_file = tmp_path / "text.txt"
    text_file.write_text("hello\nworld\n")
    binary_file = tmp_path / "launcher"
    binary = b"#!/bin/sh\n" + bytes(range(256))
    binary_file.write_bytes(binary)

    text_result = subprocess.run(
        [str(wrapper), str(text_file)], check=True, capture_output=True, text=True
    )
    binary_result = subprocess.run(
        [str(wrapper), str(binary_file)], check=True, capture_output=True, text=True
    )

    assert text_result.stdout == "hello\nworld\n"
    assert binary_result.stdout == (
        f"[binary output omitted by harness: {len(binary)} bytes, "
        f"sha256={hashlib.sha256(binary).hexdigest()}]\n"
    )
    assert "\x00" not in binary_result.stdout


def test_required_task_env_is_read_from_task_toml(tmp_path):
    task = tmp_path / "family-2"
    task.mkdir()
    (task / "task.toml").write_text(
        '[environment]\nrequired_env = ["GH_TOKEN", "SECOND_TOKEN"]\n'
    )
    assert METHOD._required_task_env(task) == ["GH_TOKEN", "SECOND_TOKEN"]


@pytest.mark.parametrize("instance_count", [2, 3, 5, 6])
def test_evaluate_heldouts_covers_all_real_family_instances(monkeypatch, tmp_path, instance_count):
    seen = []

    family = tmp_path / "family"
    for number in range(1, instance_count + 1):
        instance = family / f"family-{number}"
        instance.mkdir(parents=True)
        (instance / "instruction.md").write_text(f"instance {number}")

    def fake_heldout_eval(**kwargs):
        seen.append(kwargs["instance_number"])
        return {"instance_id": f"family-{kwargs['instance_number']}"}

    monkeypatch.setattr(METHOD, "_heldout_eval", fake_heldout_eval)
    results = METHOD._evaluate_heldouts(
        task_path=family / "family-1",
        trial_path=tmp_path,
        frozen=tmp_path / "frozen",
        agent={},
        model_name="model",
        max_steps=1,
    )
    assert seen == list(range(2, instance_count + 1))
    assert [item["instance_id"] for item in results] == [
        f"family-{number}" for number in range(2, instance_count + 1)
    ]
