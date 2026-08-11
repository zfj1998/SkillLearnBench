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


def test_accepts_strict_prefix_parent_and_tool_links(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    records = [
        _record("user", "u1", None, text="solve"),
        _record("assistant", "a1", "u1", content=[{"type": "tool_use", "id": "t1"}]),
        _record("user", "u2", "a1", content=[{"type": "tool_result", "tool_use_id": "t1"}]),
        _record("assistant", "a2", "u2", text="done"),
    ]
    _write(first, records)
    _write(second, records + [
        _record("user", "u3", "a2", text="reflect"),
        _record("assistant", "a3", "u3", text="captured"),
    ])
    result = METHOD._audit_session_snapshot(
        second, session_id=SESSION, expected_prompt="reflect", previous_path=first
    )
    assert result["prefix_verified"] is True
    assert result["parent_links_verified"] is True
    assert result["tool_links_verified"] is True
    assert result["appended_records"] == 2


def test_rejects_non_prefix_snapshot(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write(first, [_record("user", "u1", None, text="solve")])
    _write(second, [_record("user", "different", None, text="reflect")])
    with pytest.raises(RuntimeError, match="strict byte prefix"):
        METHOD._audit_session_snapshot(
            second, session_id=SESSION, expected_prompt="reflect", previous_path=first
        )


def test_rejects_parent_branch_even_when_bytes_are_prefix(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    records = [
        _record("user", "u1", None, text="solve"),
        _record("assistant", "a1", "u1", text="done"),
    ]
    _write(first, records)
    _write(second, records + [_record("user", "u2", "u1", text="reflect")])
    with pytest.raises(RuntimeError, match="Non-contiguous"):
        METHOD._audit_session_snapshot(
            second, session_id=SESSION, expected_prompt="reflect", previous_path=first
        )


def test_rejects_unresolved_tool_call(tmp_path):
    path = tmp_path / "session.jsonl"
    _write(path, [
        _record("user", "u1", None, text="solve"),
        _record("assistant", "a1", "u1", content=[{"type": "tool_use", "id": "t1"}]),
    ])
    with pytest.raises(RuntimeError, match="unresolved tool calls"):
        METHOD._audit_session_snapshot(path, session_id=SESSION, expected_prompt="solve")


def test_rejects_missing_exact_appended_prompt(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    records = [_record("user", "u1", None, text="solve")]
    _write(first, records)
    _write(second, records + [_record("assistant", "a1", "u1", text="reflect")])
    with pytest.raises(RuntimeError, match="exactly one appended user prompt"):
        METHOD._audit_session_snapshot(
            second, session_id=SESSION, expected_prompt="reflect", previous_path=first
        )
