"""Three verifier-backed attempts and a terminal Skill Creator reflection.

The solve/repair/reflection turns share one explicit Claude Code session.  The
agent container never receives the tests mount: every verifier invocation runs
against a committed filesystem snapshot in a disposable sibling container.
"""

from __future__ import annotations

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
    import hashlib
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
        rc, out, err, steps = _claude_turn(
            container=container_name, agent=agent, model=model_name, prompt=prompt,
            session_id=session_id, resume=attempt > 1, max_steps=max_steps,
            output_path=str(phase_dir / "agent.jsonl"), task_path=task_path,
        )
        total_steps += steps
        stdout_parts.append(out)
        stderr_parts.append(err)
        (phase_dir / "agent-exit.txt").write_text(str(rc), encoding="utf-8")
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
    rc, out, err, steps = _claude_turn(
        container=container_name, agent=agent, model=model_name, prompt=reflection,
        session_id=session_id, resume=True, max_steps=max_steps,
        output_path=str(trial_path / "reflection.jsonl"), task_path=task_path,
    )
    total_steps += steps
    stdout_parts.append(out)
    stderr_parts.append(err)
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
        "heldout": heldout,
    }
    (trial_path / "selfgen_audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    # The plugin contract's rounds field is the scientific solve-attempt count.
    return passed, total_steps, "".join(stdout_parts), "".join(stderr_parts), audit["attempts_used"]
