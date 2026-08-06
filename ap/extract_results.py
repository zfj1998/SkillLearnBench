#!/usr/bin/env python3
"""Extract trial-level SkillLearnBench results from AP job/group exports.

The extractor deliberately preserves infrastructure failures.  A row is only
eligible for scoring when Harbor produced a verifier reward, no exception, and
the agent made a real model call.  ``audit_results.py`` applies the stricter
coverage and one-selected-trial-per-condition checks.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


CONDITIONS = {"no_skill", "human_authored"}


def agent_log_usage(trial_dir: Path) -> tuple[int | None, int | None, bool | None]:
    """Recover usage and terminal success when Harbor aggregation is absent."""
    path = trial_dir / "agent" / "claude-code.txt"
    if not path.is_file():
        return None, None, None
    input_tokens = 0
    output_tokens = 0
    saw_usage = False
    terminal_success: bool | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            usage = (event.get("message") or {}).get("usage") or {}
            current_input = usage.get("input_tokens")
            current_output = usage.get("output_tokens")
            if isinstance(current_input, int) and isinstance(current_output, int):
                input_tokens += current_input
                output_tokens += current_output
                saw_usage = True
        elif event.get("type") == "result":
            terminal_success = (
                event.get("is_error") is False
                and event.get("terminal_reason") not in {"api_error", "error"}
            )
    if not saw_usage:
        return None, None, terminal_success
    return input_tokens, output_tokens, terminal_success


def trial_results(root: Path):
    for path in sorted(root.rglob("result.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(result.get("task_id"), dict) and (
            "agent_result" in result or "exception_info" in result
        ):
            yield path, result


def parse_input(value: str) -> tuple[str, Path]:
    try:
        condition, raw_path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected CONDITION=PATH") from exc
    if condition not in CONDITIONS:
        raise argparse.ArgumentTypeError(
            f"condition must be one of {sorted(CONDITIONS)}, got {condition!r}"
        )
    path = Path(raw_path)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"export directory does not exist: {path}")
    return condition, path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        type=parse_input,
        metavar="CONDITION=PATH",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    rows = []
    seen_sources: set[Path] = set()
    for condition, root in args.input:
        for path, result in trial_results(root):
            resolved = path.resolve()
            if resolved in seen_sources:
                continue
            seen_sources.add(resolved)
            agent_result = result.get("agent_result") or {}
            verifier_result = result.get("verifier_result") or {}
            rewards = verifier_result.get("rewards") or {}
            reward = rewards.get("reward")
            input_tokens = agent_result.get("n_input_tokens")
            output_tokens = agent_result.get("n_output_tokens")
            exception = result.get("exception_info")
            terminal_success = None
            if not isinstance(input_tokens, int) or input_tokens <= 0:
                input_tokens, output_tokens, terminal_success = agent_log_usage(path.parent)
            infrastructure_failure = (
                exception is not None
                or reward not in (0, 0.0, 1, 1.0)
                or isinstance(reward, bool)
                or not isinstance(input_tokens, int)
                or input_tokens <= 0
                or terminal_success is False
            )
            rows.append(
                {
                    "instance_id": result["task_id"].get("path"),
                    "condition": condition,
                    "reward": reward,
                    "infrastructure_failure": infrastructure_failure,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "exception_info": exception,
                    "terminal_success": terminal_success,
                    "trial_id": result.get("id"),
                    "result_path": str(path),
                }
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "trial_count": len(rows),
                "infrastructure_failures": sum(
                    bool(row["infrastructure_failure"]) for row in rows
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
