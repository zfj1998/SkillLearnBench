#!/usr/bin/env python3
"""Fail-closed scorer for selected SkillLearnBench AP trials."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONDITIONS = ("no_skill", "human_authored")
INSTANCE_RE = re.compile(r"^(?P<family>.+)-(?P<index>[1-9][0-9]*)$")


def expected_instances() -> dict[str, str]:
    expected: dict[str, str] = {}
    for path in sorted((ROOT / "tasks").glob("*/*/task.toml")):
        expected[path.parent.name] = path.parent.parent.name
    if len(expected) != 100 or len(set(expected.values())) != 20:
        raise RuntimeError("source checkout is not the expected 20-task/100-instance set")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    expected = expected_instances()
    rows: dict[tuple[str, str], dict] = {}

    for line_number, line in enumerate(args.input.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        instance = row.get("instance_id")
        condition = row.get("condition")
        key = (condition, instance)
        if condition not in CONDITIONS:
            raise SystemExit(f"line {line_number}: foreign condition {condition!r}")
        if instance not in expected:
            raise SystemExit(f"line {line_number}: foreign instance {instance!r}")
        if key in rows:
            raise SystemExit(f"line {line_number}: duplicate selected trial {key}")
        if row.get("infrastructure_failure"):
            raise SystemExit(f"line {line_number}: infrastructure failure selected for {key}")
        reward = row.get("reward")
        if isinstance(reward, bool) or reward not in (0, 0.0, 1, 1.0):
            raise SystemExit(f"line {line_number}: non-binary verifier reward for {key}: {reward!r}")
        rows[key] = row

    required = {(condition, instance) for condition in CONDITIONS for instance in expected}
    missing = sorted(required - rows.keys())
    extra = sorted(rows.keys() - required)
    if missing or extra:
        raise SystemExit(
            f"coverage failure: selected={len(rows)} required={len(required)} "
            f"missing={missing[:10]} extra={extra[:10]}"
        )

    task_scores: dict[str, dict[str, float]] = {condition: {} for condition in CONDITIONS}
    for condition in CONDITIONS:
        by_family: dict[str, list[float]] = defaultdict(list)
        for instance, family in expected.items():
            by_family[family].append(float(rows[(condition, instance)]["reward"]))
        task_scores[condition] = {
            family: sum(rewards) / len(rewards)
            for family, rewards in sorted(by_family.items())
        }

    macro = {
        condition: sum(scores.values()) / len(scores)
        for condition, scores in task_scores.items()
    }
    per_task = {
        family: {
            "instances": sum(1 for value in expected.values() if value == family),
            "no_skill": task_scores["no_skill"][family],
            "human_authored": task_scores["human_authored"][family],
            "human_minus_no_skill": (
                task_scores["human_authored"][family] - task_scores["no_skill"][family]
            ),
        }
        for family in sorted(set(expected.values()))
    }
    report = {
        "schema_version": "1.0",
        "selected_trial_count": len(rows),
        "instance_count_per_condition": len(expected),
        "task_family_count": len(per_task),
        "aggregation": "task-inner mean then equal-weight mean across 20 tasks",
        "macro": {
            **macro,
            "human_minus_no_skill": macro["human_authored"] - macro["no_skill"],
        },
        "per_task": per_task,
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
