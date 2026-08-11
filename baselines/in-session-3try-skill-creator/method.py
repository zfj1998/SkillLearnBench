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
import uuid
from pathlib import Path
from typing import Any


_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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
    for record in records:
        record_session = record.get("sessionId")
        if record_session is not None and record_session != session_id:
            raise RuntimeError(
                f"Claude transcript session mismatch: expected {session_id}, saw {record_session}"
            )
        if record.get("type") == "last-prompt":
            leaf_uuid = record.get("leafUuid")
            if not isinstance(leaf_uuid, str) or leaf_uuid not in uuids:
                raise RuntimeError(f"Invalid Claude transcript last-prompt leaf: {leaf_uuid}")
            last_prompt_leaves.append((len(main_records), leaf_uuid))

        record_uuid = record.get("uuid")
        if not isinstance(record_uuid, str) or not record_uuid:
            continue
        if record_uuid in uuids:
            raise RuntimeError(f"Duplicate Claude transcript uuid: {record_uuid}")
        parent = record.get("parentUuid")
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
        if not previous_leaves or prompt_records[0].get("parentUuid") != previous_leaves[-1]:
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
    }


def _export_and_audit_session(
    *, container: str, session_id: str, destination: Path, expected_prompt: str,
    previous_path: Path | None,
) -> dict[str, Any]:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise RuntimeError(f"Unsafe Claude session id: {session_id!r}")
    find_result = subprocess.run(
        [
            "docker", "exec", container, "sh", "-c",
            'root="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"; '
            'test -d "$root/projects" && find "$root/projects" -type f -name "$1.jsonl" -print',
            "sh", session_id,
        ],
        capture_output=True, text=True, check=True,
    )
    matches = [line.strip() for line in find_result.stdout.splitlines() if line.strip()]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one native Claude transcript for {session_id}; found {len(matches)}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["docker", "cp", f"{container}:{matches[0]}", str(destination)],
        capture_output=True, text=True, check=True,
    )
    return _audit_session_snapshot(
        destination, session_id=session_id, expected_prompt=expected_prompt,
        previous_path=previous_path,
    )


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
    command = (
        "claude --verbose --output-format stream-json "
        f"--model {shlex.quote(model)} --max-turns {int(max_steps)} "
        f"{session_flag} -p \"$(cat {prompt_path})\" "
        f"--allowedTools {tools}"
    )
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-c", command],
        capture_output=True,
        text=True,
        timeout=3600,
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


def _validate_skills(container: str, workdir: str) -> list[str]:
    result = subprocess.run(
        ["docker", "exec", container, "find", f"{workdir}/environment/skills", "-type", "f", "-name", "SKILL.md"],
        capture_output=True,
        text=True,
    )
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not 1 <= len(paths) <= 5:
        raise RuntimeError(f"Skill Creator must produce 1-5 SKILL.md files; found {len(paths)}")
    for path in paths:
        content = subprocess.run(
            ["docker", "exec", container, "cat", path], capture_output=True, text=True, check=True
        ).stdout
        if not content.startswith("---") or not re.search(r"(?m)^name:\s*\S+", content) or not re.search(r"(?m)^description:\s*\S+", content):
            raise RuntimeError(f"Invalid skill frontmatter: {path}")
    return paths


def _heldout_eval(
    *, generation_container: str, task_path: Path, trial_path: Path,
    agent: dict[str, Any], model_name: str, task_workdir: str, max_steps: int,
) -> dict[str, Any]:
    family = task_path.parent.name
    heldout_path = task_path.parent / f"{family}-2"
    if not (heldout_path / "instruction.md").exists():
        raise RuntimeError(f"Held-out instance is missing: {heldout_path}")

    frozen = trial_path / "frozen-skills"
    frozen.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["docker", "cp", f"{generation_container}:{task_workdir}/environment/skills/.", str(frozen)],
        check=True, capture_output=True, text=True,
    )
    frozen_hashes: dict[str, str] = {}
    for skill in sorted(frozen.rglob("SKILL.md")):
        frozen_hashes[str(skill.relative_to(frozen))] = hashlib.sha256(skill.read_bytes()).hexdigest()

    build_root = Path(tempfile.mkdtemp(prefix="selfgen_heldout_build_"))
    image = f"skilllearn-selfgen-heldout-{uuid.uuid4().hex[:12]}"
    container = f"skilllearn-selfgen-heldout-{uuid.uuid4().hex[:12]}"
    heldout_log = trial_path / "heldout-instance-2"
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
        for name in list(agent.get("env", [])) + list(agent.get("passthrough_env", [])):
            value = os.environ.get(name)
            if value:
                env_args.extend(["-e", f"{name}={value}"])
        subprocess.run(
            ["docker", "run", "-d", "--name", container, "-v", f"{heldout_log}:/logs", *env_args, image, "sleep", "3600"],
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


def run(
    *, container_name: str, task_path: Path, trial_path: Path, agent: dict,
    model_name: str, instruction: str, task_workdir: str, max_rounds: int,
    max_steps: int,
) -> tuple[bool, int, str, str, int]:
    session_id = str(uuid.uuid4())
    audit_dir = trial_path / "same-session-attempts"
    audit_dir.mkdir(parents=True, exist_ok=True)
    attempts = max(1, min(int(max_rounds), 3))
    total_steps = 0
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    passed = False
    feedback = ""
    verifier_exit = 0
    session_snapshots: list[dict[str, Any]] = []
    previous_session_path: Path | None = None

    for attempt in range(1, attempts + 1):
        if attempt == 1:
            prompt = instruction
        else:
            prompt = (
                "Continue solving the same task in this same session. The hidden official verifier "
                "was run in an isolated container. You may use only this bounded feedback; do not "
                "look for verifier sources or logs.\n\n" + feedback
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
            container=container_name, task_path=task_path, trial_path=trial_path, attempt=attempt
        )
        (phase_dir / "bounded-feedback.txt").write_text(feedback, encoding="utf-8")
        if passed:
            break

    reflection = (
        "Now stop modifying the task solution. Use the preloaded skill-creator skill to capture "
        "reusable knowledge from all solve attempts and verifier feedback in this session. Write "
        f"1-5 skills under {task_workdir}/environment/skills/<skill-name>/SKILL.md. Generalize to "
        "sibling instances; do not include the specific poem or hidden-test guesses."
    )
    (trial_path / "reflection-prompt.txt").write_text(reflection, encoding="utf-8")
    rc, out, err, steps = _claude_turn(
        container=container_name, agent=agent, model=model_name, prompt=reflection,
        session_id=session_id, resume=True, max_steps=max_steps,
        output_path=str(trial_path / "reflection.jsonl"), task_path=task_path,
    )
    total_steps += steps
    stdout_parts.append(out)
    stderr_parts.append(err)
    reflection_session_path = trial_path / "reflection-session.jsonl"
    session_snapshots.append(_export_and_audit_session(
        container=container_name, session_id=session_id, destination=reflection_session_path,
        expected_prompt=reflection, previous_path=previous_session_path,
    ))
    skills = _validate_skills(container_name, task_workdir)
    heldout = _heldout_eval(
        generation_container=container_name, task_path=task_path, trial_path=trial_path,
        agent=agent, model_name=model_name, task_workdir=task_workdir, max_steps=max_steps,
    )
    audit = {
        "protocol": "in-session-3try-skill-creator",
        "scoreable": False,
        "session_id": session_id,
        "attempts_used": len(list(audit_dir.glob("attempt-*"))),
        "terminal_verifier_passed": passed,
        "reflection_exit": rc,
        "skills": skills,
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
        "heldout": heldout,
    }
    (trial_path / "selfgen_audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    # The plugin contract's rounds field is the scientific solve-attempt count.
    return passed, total_steps, "".join(stdout_parts), "".join(stderr_parts), audit["attempts_used"]
