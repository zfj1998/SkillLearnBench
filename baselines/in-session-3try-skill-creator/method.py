"""Three verifier-backed attempts and a terminal Skill Creator reflection.

The solve/repair/reflection turns share one explicit Claude Code session.  The
agent container never receives the tests mount: every verifier invocation runs
against a committed filesystem snapshot in a disposable sibling container.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_CLAUDE_TURN_TIMEOUT_SECONDS = 7200
_HELDOUT_CONTAINER_KEEPALIVE_SECONDS = 10800
_ABLATION_MODES = {
    "standard",
    "prompt-only",
    "family-only",
    "trajectory-summary",
}
_BINARY_SAFE_CAT = r'''#!/bin/sh
set -u
if [ "$#" -eq 0 ]; then
  exec /usr/bin/cat
fi
tmp="$(mktemp /tmp/selfgen-cat.XXXXXX)" || exit 1
trap 'rm -f "$tmp"' EXIT HUP INT TERM
/usr/bin/cat "$@" >"$tmp"
rc=$?
if [ ! -s "$tmp" ] || LC_ALL=C grep -Iq . "$tmp"; then
  /usr/bin/cat "$tmp"
else
  bytes="$(wc -c <"$tmp" | tr -d '[:space:]')"
  digest="$(sha256sum "$tmp" | awk '{print $1}')"
  printf '[binary output omitted by harness: %s bytes, sha256=%s]\n' "$bytes" "$digest"
fi
exit "$rc"
'''


def _scoreable_mode() -> bool:
    value = os.environ.get("SELFGEN_SCOREABLE", "false").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError("SELFGEN_SCOREABLE must be true or false")
    return value == "true"


def _ablation_mode() -> str:
    value = os.environ.get("SELFGEN_ABLATION_MODE", "standard").strip().lower()
    if value not in _ABLATION_MODES:
        raise RuntimeError(
            "SELFGEN_ABLATION_MODE must be one of: " + ", ".join(sorted(_ABLATION_MODES))
        )
    return value


def _restricted_agent(agent: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    restricted = dict(agent)
    original_tools = list(agent.get("default_tools", []))
    restricted["default_tools"] = [
        tool for tool in original_tools if tool in allowed
    ]
    # Claude Code's --allowedTools controls permission prompts; it does not
    # remove other tools from the model-visible schema. Pair it with an
    # explicit --disallowedTools list for isolation ablations.
    restricted["hard_disallowed_tools"] = [
        tool for tool in original_tools if tool not in allowed
    ]
    return restricted


def _agent_tool_names(path: Path) -> list[str]:
    names: list[str] = []
    for record in _jsonl_records(path):
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "tool_use"
                and isinstance(part.get("name"), str)
            ):
                names.append(part["name"])
    return names


def _install_binary_safe_cat(container: str) -> None:
    """Prevent raw binary Bash output from corrupting Claude JSONL transcripts."""
    subprocess.run(
        [
            "docker", "exec", "-i", container, "sh", "-c",
            "tmp=$(mktemp /usr/local/bin/cat.XXXXXX) && "
            "/usr/bin/cat >\"$tmp\" && chmod 0755 \"$tmp\" && mv \"$tmp\" /usr/local/bin/cat",
        ],
        input=_BINARY_SAFE_CAT,
        capture_output=True,
        text=True,
        check=True,
    )


def _jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    # JSON permits literal Unicode line/paragraph separators inside strings.
    # str.splitlines() treats U+2028/U+2029 as record boundaries even though
    # JSONL is delimited only by physical LF bytes, corrupting valid Claude
    # transcript records that contain those characters.
    for line_number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid Claude session JSONL at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"Non-object Claude session record at {path}:{line_number}")
        records.append(value)
    if not records:
        raise RuntimeError(f"Empty Claude session export: {path}")
    return records


def _message_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"] for part in content
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
    )


def _tool_links(records: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    calls: set[str] = set()
    results: set[str] = set()
    for record in records:
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use" and isinstance(part.get("id"), str):
                if part["id"] in calls:
                    raise RuntimeError(f"Duplicate tool_use id in Claude session: {part['id']}")
                calls.add(part["id"])
            if part.get("type") == "tool_result" and isinstance(part.get("tool_use_id"), str):
                tool_id = part["tool_use_id"]
                if tool_id not in calls:
                    raise RuntimeError(f"Tool result precedes or lacks tool_use: {tool_id}")
                results.add(tool_id)
    return calls, results


def _audit_session_snapshot(
    path: Path, *, session_id: str, expected_prompt: str,
    previous_path: Path | None = None,
) -> dict[str, Any]:
    """Validate one append-only Claude transcript snapshot, failing closed."""
    raw = path.read_bytes()
    previous_records: list[dict[str, Any]] = []
    previous_raw = b""
    if previous_path is not None:
        previous_raw = previous_path.read_bytes()
        previous_records = _jsonl_records(previous_path)
        if len(raw) <= len(previous_raw) or not raw.startswith(previous_raw):
            raise RuntimeError("Claude session snapshot is not a strict byte prefix extension")

    records = _jsonl_records(path)
    uuids: set[str] = set()
    parents: dict[str, str | None] = {}
    main_records: list[dict[str, Any]] = []
    last_prompt_leaves: list[tuple[int, str]] = []
    compact_boundaries = 0
    for record in records:
        record_session = record.get("sessionId")
        if record_session is not None and record_session != session_id:
            raise RuntimeError(
                f"Claude transcript session mismatch: expected {session_id}, saw {record_session}"
            )
        if record.get("type") == "last-prompt":
            leaf_uuid = record.get("leafUuid")
            if not isinstance(leaf_uuid, str) or not leaf_uuid:
                raise RuntimeError(f"Invalid Claude transcript last-prompt leaf: {leaf_uuid}")
            last_prompt_leaves.append((len(main_records), leaf_uuid))

        record_uuid = record.get("uuid")
        if not isinstance(record_uuid, str) or not record_uuid:
            continue
        if record_uuid in uuids:
            raise RuntimeError(f"Duplicate Claude transcript uuid: {record_uuid}")
        parent = record.get("parentUuid")
        if (
            parent is None
            and record.get("type") == "system"
            and record.get("subtype") == "compact_boundary"
        ):
            logical_parent = record.get("logicalParentUuid")
            metadata = record.get("compactMetadata")
            preserved = metadata.get("preservedSegment") if isinstance(metadata, dict) else None
            preserved_tail = preserved.get("tailUuid") if isinstance(preserved, dict) else None
            if (
                not isinstance(logical_parent, str)
                or logical_parent not in uuids
                or preserved_tail != logical_parent
            ):
                raise RuntimeError(
                    f"Invalid Claude compact boundary link: {record_uuid} -> {logical_parent}"
                )
            # Claude Code starts a new physical parent tree after automatic
            # compaction. logicalParentUuid is the continuity edge back to the
            # pre-compaction transcript and is what resume semantics preserve.
            parent = logical_parent
            compact_boundaries += 1
        if parent is not None and parent not in uuids:
            raise RuntimeError(f"Broken Claude transcript parent link: {record_uuid} -> {parent}")
        uuids.add(record_uuid)
        parents[record_uuid] = parent
        if record.get("isSidechain") is not True:
            if not main_records:
                if parent is not None:
                    raise RuntimeError("Claude transcript parent graph has no root")
            elif parent is None:
                raise RuntimeError(f"Unexpected second Claude transcript root: {record_uuid}")
            main_records.append(record)
    if not main_records or not last_prompt_leaves:
        raise RuntimeError("Claude transcript lacks main messages or a terminal leaf marker")
    missing_leaves = sorted({leaf for _, leaf in last_prompt_leaves if leaf not in uuids})
    if missing_leaves:
        raise RuntimeError(f"Invalid Claude transcript last-prompt leaf: {missing_leaves[0]}")

    previous_count = len(previous_records)
    appended = records[previous_count:]
    prompt_records = [
        record for record in appended
        if record.get("type") == "user" and _message_text(record) == expected_prompt
    ]
    if len(prompt_records) != 1:
        raise RuntimeError(
            f"Expected exactly one appended user prompt in Claude transcript; found {len(prompt_records)}"
        )
    prompt_uuid = prompt_records[0].get("uuid")
    if not isinstance(prompt_uuid, str) or prompt_uuid not in parents:
        raise RuntimeError("Appended Claude phase prompt lacks a valid UUID")

    if previous_path is not None:
        previous_leaves = [
            record.get("leafUuid") for record in previous_records
            if record.get("type") == "last-prompt"
        ]
        previous_leaf = previous_leaves[-1] if previous_leaves else None
        cursor = prompt_records[0].get("parentUuid")
        prompt_ancestors: set[str] = set()
        while cursor is not None:
            if cursor in prompt_ancestors:
                raise RuntimeError(f"Cycle in Claude transcript parent graph at {cursor}")
            prompt_ancestors.add(cursor)
            cursor = parents[cursor]
        if previous_leaf is None or previous_leaf not in prompt_ancestors:
            raise RuntimeError("Claude phase prompt does not continue the previous terminal leaf")

    leaf_uuid = last_prompt_leaves[-1][1]
    cursor: str | None = leaf_uuid
    ancestors: set[str] = set()
    while cursor is not None:
        if cursor in ancestors:
            raise RuntimeError(f"Cycle in Claude transcript parent graph at {cursor}")
        ancestors.add(cursor)
        cursor = parents[cursor]
    if prompt_uuid not in ancestors:
        raise RuntimeError("Claude terminal leaf does not descend from the appended phase prompt")

    calls, results = _tool_links(records)
    missing_results = sorted(calls - results)
    if missing_results:
        raise RuntimeError(f"Claude transcript has unresolved tool calls: {missing_results[:5]}")

    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "records": len(records),
        "appended_records": len(records) - previous_count,
        "main_graph_messages": len(main_records),
        "leaf_uuid": leaf_uuid,
        "parent_links_verified": True,
        "prefix_verified": previous_path is None or True,
        "prompt_verified": True,
        "tool_links_verified": True,
        "tool_calls": len(calls),
        "compact_boundaries": compact_boundaries,
    }


def _export_and_audit_session(
    *, container: str, session_id: str, destination: Path, expected_prompt: str,
    previous_path: Path | None,
) -> dict[str, Any]:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise RuntimeError(f"Unsafe Claude session id: {session_id!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error = "native transcript was not found"
    for retry in range(12):
        find_result = subprocess.run(
            [
                "docker", "exec", container, "sh", "-c",
                'root="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"; '
                'if test -d "$root/projects"; then '
                'find "$root/projects" -type f -name "$1.jsonl" -print; fi',
                "sh", session_id,
            ],
            capture_output=True, text=True, check=False,
        )
        matches = [line.strip() for line in find_result.stdout.splitlines() if line.strip()]
        if len(matches) == 1:
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                copy_result = subprocess.run(
                    ["docker", "cp", f"{container}:{matches[0]}", str(temporary)],
                    capture_output=True, text=True, check=False,
                )
                if copy_result.returncode != 0:
                    last_error = f"docker cp failed: {copy_result.stderr.strip()[:300]}"
                else:
                    result = _audit_session_snapshot(
                        temporary, session_id=session_id, expected_prompt=expected_prompt,
                        previous_path=previous_path,
                    )
                    os.replace(temporary, destination)
                    result["path"] = str(destination)
                    result["export_retries"] = retry
                    return result
            except RuntimeError as exc:
                # Claude Code can still be flushing a large JSONL record after
                # its CLI process exits. Retry a fresh atomic copy; never accept
                # or repair a malformed snapshot in place.
                last_error = str(exc)
                if retry == 11 and temporary.exists():
                    invalid = destination.with_name(f"{destination.stem}.invalid.jsonl")
                    os.replace(temporary, invalid)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            last_error = (
                f"Expected one native Claude transcript for {session_id}; "
                f"found {len(matches)} (find exit {find_result.returncode})"
            )
        if retry < 11:
            time.sleep(1)
    raise RuntimeError(f"Could not export a valid Claude transcript: {last_error}")


def _allowed_tools(agent: dict[str, Any], task_path: Path) -> str:
    tools = list(agent.get("default_tools", []))
    disallowed: set[str] = set()
    config = task_path / "task.toml"
    if config.exists():
        try:
            import tomllib
            with config.open("rb") as handle:
                disallowed = set(tomllib.load(handle).get("agent", {}).get("disallowed_tools", []))
        except Exception:
            pass
    return " ".join(tool for tool in tools if tool not in disallowed)


def _required_task_env(task_path: Path) -> list[str]:
    config = task_path / "task.toml"
    if not config.is_file():
        return []
    import tomllib
    with config.open("rb") as handle:
        required = tomllib.load(handle).get("environment", {}).get("required_env", [])
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise RuntimeError(f"Malformed required_env in {config}")
    return required


def _copy_text(container: str, destination: str, text: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as handle:
        handle.write(text)
        source = Path(handle.name)
    try:
        subprocess.run(
            ["docker", "cp", str(source), f"{container}:{destination}"],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        source.unlink(missing_ok=True)


def _claude_turn(
    *, container: str, agent: dict[str, Any], model: str, prompt: str,
    session_id: str, resume: bool, max_steps: int, output_path: str,
    task_path: Path,
) -> tuple[int, str, str, int]:
    prompt_path = "/tmp/selfgen-followup.txt" if resume else "/tmp/selfgen-solve.txt"
    _copy_text(container, prompt_path, prompt)
    session_flag = f"--resume {shlex.quote(session_id)}" if resume else f"--session-id {shlex.quote(session_id)}"
    tools = _allowed_tools(agent, task_path)
    disallowed_tools = " ".join(
        shlex.quote(tool) for tool in agent.get("hard_disallowed_tools", [])
    )
    disallowed_flag = (
        f" --disallowedTools {disallowed_tools}" if disallowed_tools else ""
    )
    command = (
        "claude --verbose --output-format stream-json "
        f"--model {shlex.quote(model)} --max-turns {int(max_steps)} "
        f"{session_flag} -p \"$(cat {prompt_path})\" "
        f"--allowedTools {tools}{disallowed_flag}"
    )
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-c", command],
        capture_output=True,
        text=True,
        timeout=_CLAUDE_TURN_TIMEOUT_SECONDS,
    )
    Path(output_path).write_text(result.stdout, encoding="utf-8")
    steps = 0
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            seen.add(sid)
        if event.get("type") == "assistant":
            steps += sum(
                1 for part in event.get("message", {}).get("content", [])
                if isinstance(part, dict) and part.get("type") == "tool_use"
            )
    if seen and seen != {session_id}:
        raise RuntimeError(f"Claude session continuity failed: expected {session_id}, saw {sorted(seen)}")
    return result.returncode, result.stdout, result.stderr, steps


def _verify_snapshot(
    *, container: str, task_path: Path, trial_path: Path, attempt: int,
) -> tuple[bool, str, int]:
    image = f"skilllearn-selfgen-verify-{uuid.uuid4().hex[:12]}"
    attempt_dir = trial_path / "same-session-attempts" / f"attempt-{attempt:02d}"
    verifier_dir = attempt_dir / "verifier"
    verifier_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["docker", "commit", container, image], check=True, capture_output=True, text=True)
    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{task_path / 'tests'}:/tests:ro",
                "-v", f"{verifier_dir}:/logs/verifier",
                image, "bash", "/tests/test.sh",
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
    finally:
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True)
    (verifier_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (verifier_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    reward_path = verifier_dir / "reward.txt"
    passed = reward_path.exists() and reward_path.read_text(encoding="utf-8").strip() == "1"
    ctrf_path = verifier_dir / "ctrf.json"
    failed: list[str] = []
    passed_count = total = 0
    if ctrf_path.exists():
        try:
            payload = json.loads(ctrf_path.read_text(encoding="utf-8"))
            summary = payload["results"]["summary"]
            passed_count, total = int(summary["passed"]), int(summary["tests"])
            for test in payload["results"].get("tests", []):
                if test.get("status") != "passed":
                    name = str(test.get("name", "unnamed-test")).split("::")[-1]
                    failed.append(re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120])
        except Exception:
            pass
    feedback = f"Official verifier: {'PASS' if passed else 'FAIL'} ({passed_count}/{total} tests passed)."
    if failed:
        feedback += " Failed checks: " + ", ".join(failed[:20]) + "."
    return passed, feedback, result.returncode


def _classify_skill_candidate(contents: dict[str, str]) -> dict[str, Any]:
    errors: list[str] = []
    if not contents:
        return {
            "valid": False,
            "status": "no_skill_created",
            "candidate_skills": [],
            "skills": [],
            "validation_errors": ["Skill Creator produced no SKILL.md files"],
        }
    if len(contents) > 5:
        errors.append(f"Skill Creator produced {len(contents)} SKILL.md files; maximum is 5")
    for path, content in sorted(contents.items()):
        if not content.startswith("---"):
            errors.append(f"missing YAML frontmatter: {path}")
        if not re.search(r"(?m)^name:\s*\S+", content):
            errors.append(f"missing frontmatter name: {path}")
        if not re.search(r"(?m)^description:\s*\S+", content):
            errors.append(f"missing frontmatter description: {path}")
    valid = not errors
    return {
        "valid": valid,
        "status": "valid" if valid else "invalid_skill_candidate",
        "candidate_skills": sorted(contents),
        "skills": sorted(contents) if valid else [],
        "validation_errors": errors,
    }


def _capture_skill_candidate(
    container: str, workdir: str, trial_path: Path,
) -> tuple[dict[str, Any], Path]:
    skill_root = f"{workdir}/environment/skills"
    subprocess.run(
        ["docker", "exec", container, "mkdir", "-p", skill_root],
        check=True, capture_output=True, text=True,
    )
    result = subprocess.run(
        ["docker", "exec", container, "find", skill_root, "-type", "f", "-name", "SKILL.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    contents: dict[str, str] = {}
    for path in paths:
        contents[path] = subprocess.run(
            ["docker", "exec", container, "cat", path], capture_output=True, text=True, check=True
        ).stdout
    candidate_dir = trial_path / "reflection-candidate"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["docker", "cp", f"{container}:{skill_root}/.", str(candidate_dir)],
        check=True, capture_output=True, text=True,
    )
    classification = _classify_skill_candidate(contents)
    unsafe_links = [
        str(path.relative_to(candidate_dir))
        for path in candidate_dir.rglob("*") if path.is_symlink()
    ]
    oversized = [
        str(path.relative_to(candidate_dir))
        for path in candidate_dir.rglob("*")
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 128 * 1024
    ]
    if unsafe_links or oversized:
        classification["valid"] = False
        classification["status"] = "invalid_skill_candidate"
        classification["skills"] = []
        classification["validation_errors"].extend(
            [f"symlink is not allowed: {path}" for path in unsafe_links]
            + [f"candidate file exceeds 128 KiB: {path}" for path in oversized]
        )
    (trial_path / "skill-candidate-validation.json").write_text(
        json.dumps(classification, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    frozen = trial_path / "frozen-skills"
    frozen.mkdir(parents=True, exist_ok=True)
    if classification["valid"]:
        shutil.copytree(candidate_dir, frozen, dirs_exist_ok=True)
    return classification, frozen


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _heldout_eval(
    *, task_path: Path, trial_path: Path, frozen: Path, instance_number: int,
    agent: dict[str, Any], model_name: str, max_steps: int,
) -> dict[str, Any]:
    family = task_path.parent.name
    heldout_path = task_path.parent / f"{family}-{instance_number}"
    if not (heldout_path / "instruction.md").exists():
        raise RuntimeError(f"Held-out instance is missing: {heldout_path}")

    frozen_hashes = _file_hashes(frozen)

    build_root = Path(tempfile.mkdtemp(prefix="selfgen_heldout_build_"))
    image = f"skilllearn-selfgen-heldout-{uuid.uuid4().hex[:12]}"
    container = f"skilllearn-selfgen-heldout-{uuid.uuid4().hex[:12]}"
    heldout_log = trial_path / f"heldout-instance-{instance_number}"
    heldout_log.mkdir(parents=True, exist_ok=True)
    try:
        build_env = build_root / "environment"
        shutil.copytree(heldout_path / "environment", build_env, ignore=shutil.ignore_patterns("skills"))
        shutil.copytree(frozen, build_env / "skills")
        subprocess.run(
            ["docker", "build", "-t", image, str(build_env)],
            check=True, capture_output=True, text=True, timeout=1800,
        )
        env_args: list[str] = []
        required_env = _required_task_env(heldout_path)
        env_names = (
            list(agent.get("env", []))
            + list(agent.get("passthrough_env", []))
            + required_env
        )
        for name in dict.fromkeys(env_names):
            value = os.environ.get(name)
            if not value and name in required_env:
                raise RuntimeError(f"Held-out {heldout_path.name} requires ${name}")
            if value:
                env_args.extend(["-e", f"{name}={value}"])
        subprocess.run(
            [
                "docker", "run", "-d", "--name", container,
                "-v", f"{heldout_log}:/logs", *env_args, image,
                "sleep", str(_HELDOUT_CONTAINER_KEEPALIVE_SECONDS),
            ],
            check=True, capture_output=True, text=True,
        )
        runtime_deps = agent.get("runtime_deps")
        if runtime_deps:
            subprocess.run(
                ["docker", "exec", container, "sh", "-c", runtime_deps],
                check=True, capture_output=True, text=True, timeout=900,
            )
        subprocess.run(
            ["docker", "exec", container, "sh", "-c", agent["install"]],
            check=True, capture_output=True, text=True, timeout=900,
        )
        _install_binary_safe_cat(container)
        heldout_session = str(uuid.uuid4())
        instruction = (heldout_path / "instruction.md").read_text(encoding="utf-8").strip()
        rc, _out, err, steps = _claude_turn(
            container=container, agent=agent, model=model_name, prompt=instruction,
            session_id=heldout_session, resume=False, max_steps=max_steps,
            output_path=str(heldout_log / "agent.jsonl"), task_path=heldout_path,
        )
        passed, feedback, verifier_exit = _verify_snapshot(
            container=container, task_path=heldout_path, trial_path=heldout_log, attempt=1,
        )
        return {
            "instance_id": heldout_path.name,
            "session_id": heldout_session,
            "agent_exit": rc,
            "agent_stderr": err[:1000],
            "steps_used": steps,
            "verifier_exit": verifier_exit,
            "verifier_passed": passed,
            "bounded_result": feedback,
            "frozen_skill_sha256": frozen_hashes,
            "hidden_tests_mounted_in_agent": False,
        }
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True)
        shutil.rmtree(build_root, ignore_errors=True)


def _evaluate_heldouts(
    *, task_path: Path, trial_path: Path, frozen: Path,
    agent: dict[str, Any], model_name: str, max_steps: int,
) -> list[dict[str, Any]]:
    heldout_numbers = _family_heldout_numbers(task_path)
    return [
        _heldout_eval(
            task_path=task_path, trial_path=trial_path, frozen=frozen,
            instance_number=instance_number, agent=agent,
            model_name=model_name, max_steps=max_steps,
        )
        for instance_number in heldout_numbers
    ]


def _family_heldout_numbers(task_path: Path) -> list[int]:
    family = task_path.parent.name
    if task_path.name != f"{family}-1":
        raise RuntimeError(f"Learning instance must be {family}-1, got {task_path.name}")
    numbered: list[int] = []
    for sibling in task_path.parent.iterdir():
        match = re.fullmatch(rf"{re.escape(family)}-(\d+)", sibling.name)
        if match and sibling.is_dir() and (sibling / "instruction.md").is_file():
            numbered.append(int(match.group(1)))
    if numbered.count(1) != 1 or len(numbered) != len(set(numbered)):
        raise RuntimeError(f"Malformed or duplicate family instance set for {family}: {numbered}")
    heldout_numbers = sorted(number for number in numbered if number != 1)
    if not heldout_numbers:
        raise RuntimeError(f"Family {family} has no held-out instances")
    return heldout_numbers


def run(
    *, container_name: str, task_path: Path, trial_path: Path, agent: dict,
    model_name: str, instruction: str, task_workdir: str, max_rounds: int,
    max_steps: int,
) -> tuple[bool, int, str, str, int]:
    _install_binary_safe_cat(container_name)
    ablation_mode = _ablation_mode()
    session_id = str(uuid.uuid4())
    audit_dir = trial_path / "same-session-attempts"
    audit_dir.mkdir(parents=True, exist_ok=True)
    total_steps = 0
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    passed: bool | None = None
    feedback = ""
    verifier_exit: int | None = None
    session_snapshots: list[dict[str, Any]] = []
    previous_session_path: Path | None = None
    generation_tool_names: list[str] = []
    generation_allowed_tools: list[str] | None = None
    generation_disallowed_tools: list[str] | None = None

    if ablation_mode in {"prompt-only", "family-only"}:
        family = task_path.parent.name
        if ablation_mode == "prompt-only":
            available_information = (
                "You may use only the following instance-1 instruction. You must not inspect or "
                "execute the task environment, and you will receive no verifier feedback.\n\n"
                + instruction
            )
        else:
            available_information = (
                "You may use only this task-family identifier: "
                f"{family!r} (human-readable: {family.replace('-', ' ')}). "
                "No concrete instance instruction, environment, solve trajectory, or verifier "
                "feedback is available."
            )
        generation_prompt = (
            available_information
            + "\n\nUse the preloaded skill-creator skill to write 1-5 reusable skills under "
            f"{task_workdir}/environment/skills/<skill-name>/SKILL.md for future sibling "
            "instances. Do not claim knowledge you were not given, and do not solve or inspect "
            "the current environment."
        )
        (trial_path / "generation-prompt.txt").write_text(
            generation_prompt, encoding="utf-8"
        )
        generation_agent = _restricted_agent(agent, {"Skill", "Write"})
        generation_allowed_tools = generation_agent["default_tools"]
        generation_disallowed_tools = generation_agent["hard_disallowed_tools"]
        rc, out, err, steps = _claude_turn(
            container=container_name, agent=generation_agent, model=model_name,
            prompt=generation_prompt, session_id=session_id, resume=False,
            max_steps=max_steps, output_path=str(trial_path / "generation.jsonl"),
            task_path=task_path,
        )
        total_steps += steps
        stdout_parts.append(out)
        stderr_parts.append(err)
        generation_session_path = trial_path / "generation-session.jsonl"
        session_snapshots.append(_export_and_audit_session(
            container=container_name, session_id=session_id,
            destination=generation_session_path, expected_prompt=generation_prompt,
            previous_path=None,
        ))
        generation_tool_names = _agent_tool_names(trial_path / "generation.jsonl")
        forbidden = sorted(set(generation_tool_names) - set(generation_allowed_tools))
        if forbidden:
            raise RuntimeError(
                "Generation-only ablation accessed forbidden tools: " + ", ".join(forbidden)
            )
        if rc != 0:
            raise RuntimeError(f"Generation-only skill turn failed with agent exit {rc}")
    else:
        attempts = max(1, min(int(max_rounds), 3))
        passed = False
        for attempt in range(1, attempts + 1):
            if attempt == 1:
                prompt = instruction
            else:
                prompt = (
                    "Continue solving the same task in this same session. The hidden official "
                    "verifier was run in an isolated container. You may use only this bounded "
                    "feedback; do not look for verifier sources or logs.\n\n" + feedback
                )
            phase_dir = audit_dir / f"attempt-{attempt:02d}"
            phase_dir.mkdir(parents=True, exist_ok=True)
            (phase_dir / "phase-prompt.txt").write_text(prompt, encoding="utf-8")
            rc, out, err, steps = _claude_turn(
                container=container_name, agent=agent, model=model_name, prompt=prompt,
                session_id=session_id, resume=attempt > 1, max_steps=max_steps,
                output_path=str(phase_dir / "agent.jsonl"), task_path=task_path,
            )
            total_steps += steps
            stdout_parts.append(out)
            stderr_parts.append(err)
            (phase_dir / "agent-exit.txt").write_text(str(rc), encoding="utf-8")
            session_path = phase_dir / "claude-session.jsonl"
            session_snapshots.append(_export_and_audit_session(
                container=container_name, session_id=session_id, destination=session_path,
                expected_prompt=prompt, previous_path=previous_session_path,
            ))
            previous_session_path = session_path
            passed, feedback, verifier_exit = _verify_snapshot(
                container=container_name, task_path=task_path,
                trial_path=trial_path, attempt=attempt,
            )
            (phase_dir / "bounded-feedback.txt").write_text(feedback, encoding="utf-8")
            if passed:
                break

        if ablation_mode == "trajectory-summary":
            reflection = (
                "Now stop modifying the task solution. Without using the Skill tool or any "
                "preloaded skill-creation workflow, write an ordinary concise summary of reusable "
                "lessons from the solve attempts and bounded verifier feedback in this session. "
                "For a format-compatible held-out evaluation, save that summary as 1-5 SKILL.md "
                f"files under {task_workdir}/environment/skills/<skill-name>/SKILL.md, each with "
                "minimal YAML name and description frontmatter. Generalize to sibling instances "
                "and do not include instance-specific answers or hidden-test guesses."
            )
            reflection_agent = _restricted_agent(agent, {"Write"})
            generation_allowed_tools = reflection_agent["default_tools"]
            generation_disallowed_tools = reflection_agent["hard_disallowed_tools"]
        else:
            reflection = (
                "Now stop modifying the task solution. Use the preloaded skill-creator skill to "
                "capture reusable knowledge from all solve attempts and verifier feedback in this "
                f"session. Write 1-5 skills under {task_workdir}/environment/skills/"
                "<skill-name>/SKILL.md. Generalize to sibling instances; do not include "
                "instance-specific answers or hidden-test guesses."
            )
            reflection_agent = agent
        (trial_path / "reflection-prompt.txt").write_text(reflection, encoding="utf-8")
        rc, out, err, steps = _claude_turn(
            container=container_name, agent=reflection_agent, model=model_name,
            prompt=reflection, session_id=session_id, resume=True, max_steps=max_steps,
            output_path=str(trial_path / "reflection.jsonl"), task_path=task_path,
        )
        total_steps += steps
        stdout_parts.append(out)
        stderr_parts.append(err)
        reflection_session_path = trial_path / "reflection-session.jsonl"
        session_snapshots.append(_export_and_audit_session(
            container=container_name, session_id=session_id,
            destination=reflection_session_path, expected_prompt=reflection,
            previous_path=previous_session_path,
        ))
        generation_tool_names = _agent_tool_names(trial_path / "reflection.jsonl")
        if ablation_mode == "trajectory-summary":
            forbidden = sorted(set(generation_tool_names) - set(generation_allowed_tools or []))
            if forbidden or "Skill" in generation_tool_names:
                raise RuntimeError(
                    "Trajectory-summary ablation used a forbidden generation tool: "
                    + ", ".join(forbidden or ["Skill"])
                )
        if rc != 0:
            label = "Trajectory summary" if ablation_mode == "trajectory-summary" else "Skill Creator reflection"
            raise RuntimeError(f"{label} failed with agent exit {rc}")

    skill_candidate, frozen = _capture_skill_candidate(
        container_name, task_workdir, trial_path,
    )
    frozen_before = _file_hashes(frozen)
    heldouts = _evaluate_heldouts(
        task_path=task_path, trial_path=trial_path, frozen=frozen,
        agent=agent, model_name=model_name, max_steps=max_steps,
    )
    frozen_after = _file_hashes(frozen)
    if frozen_after != frozen_before:
        raise RuntimeError("Frozen skill library mutated during held-out evaluation")
    heldout_session_ids = [item["session_id"] for item in heldouts]
    if len(set(heldout_session_ids)) != len(heldouts) or session_id in heldout_session_ids:
        raise RuntimeError("Held-out evaluations did not use distinct fresh sessions")
    family = task_path.parent.name
    expected_heldouts = [f"{family}-{number}" for number in _family_heldout_numbers(task_path)]
    if [item["instance_id"] for item in heldouts] != expected_heldouts:
        raise RuntimeError("Held-out instance coverage or order mismatch")
    heldout_pass_count = sum(item["verifier_passed"] is True for item in heldouts)
    protocol = {
        "standard": "in-session-3try-skill-creator-family-v2",
        "prompt-only": "prompt-only-skill-creator-family-v1",
        "family-only": "family-only-skill-creator-family-v1",
        "trajectory-summary": "in-session-3try-trajectory-summary-family-v1",
    }[ablation_mode]
    attempts_used = len(list(audit_dir.glob("attempt-*")))
    audit = {
        "protocol": protocol,
        "ablation_mode": ablation_mode,
        "scoreable": _scoreable_mode(),
        "family_id": family,
        "session_id": session_id,
        "attempts_used": attempts_used,
        "terminal_verifier_passed": passed,
        "reflection_exit": rc,
        "learning_environment_executed": ablation_mode in {"standard", "trajectory-summary"},
        "learning_verifier_executed": ablation_mode in {"standard", "trajectory-summary"},
        "generation_tool_names": generation_tool_names,
        "generation_allowed_tools": generation_allowed_tools,
        "generation_disallowed_tools": generation_disallowed_tools,
        "generation_environment_access_verified": (
            ablation_mode not in {"prompt-only", "family-only"}
            or set(generation_tool_names) <= {"Skill", "Write"}
        ),
        "skill_creator_allowed": ablation_mode != "trajectory-summary",
        "skill_generation_valid": skill_candidate["valid"],
        "skill_generation_status": skill_candidate["status"],
        "skills": skill_candidate["skills"],
        "candidate_skills": skill_candidate["candidate_skills"],
        "skill_validation_errors": skill_candidate["validation_errors"],
        "frozen_skill_sha256": frozen_before,
        "frozen_library_unchanged": True,
        "hidden_tests_mounted_in_agent": False,
        "verifier_exit": verifier_exit,
        "parent_continuity_verified": all(
            item["parent_links_verified"] for item in session_snapshots
        ),
        "prefix_continuity_verified": all(
            item["prefix_verified"] for item in session_snapshots[1:]
        ),
        "prompt_continuity_verified": all(
            item["prompt_verified"] for item in session_snapshots
        ),
        "tool_link_continuity_verified": all(
            item["tool_links_verified"] for item in session_snapshots
        ),
        "session_snapshots": session_snapshots,
        "heldouts": heldouts,
        "heldout_expected": expected_heldouts,
        "heldout_pass_count": heldout_pass_count,
        "heldout_total": len(heldouts),
        "heldout_score": heldout_pass_count / len(heldouts),
    }
    (trial_path / "selfgen_audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    # The plugin contract's rounds field is the scientific solve-attempt count.
    runner_passed = True if passed is None else passed
    return runner_passed, total_steps, "".join(stdout_parts), "".join(stderr_parts), attempts_used
