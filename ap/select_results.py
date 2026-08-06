#!/usr/bin/env python3
"""Select one fail-closed result per SkillLearnBench condition/instance.

Selection is fixed before scoring: use a valid main trial when present;
otherwise require exactly one valid retry. Multiple valid retries are rejected
instead of choosing the highest reward.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONDITIONS = ("no_skill", "human_authored")


def read_rows(path: Path) -> list[dict]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("condition") not in CONDITIONS or not row.get("instance_id"):
            raise SystemExit(f"{path}:{line_number}: invalid result identity")
        rows.append(row)
    return rows


def is_valid(row: dict) -> bool:
    reward = row.get("reward")
    return (
        not row.get("infrastructure_failure")
        and not isinstance(reward, bool)
        and reward in (0, 0.0, 1, 1.0)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main", required=True, type=Path)
    parser.add_argument("--retry", action="append", default=[], type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    expected_instances = {
        path.parent.name for path in (ROOT / "tasks").glob("*/*/task.toml")
    }
    if len(expected_instances) != 100:
        raise SystemExit(f"expected 100 source instances, found {len(expected_instances)}")
    required = {
        (condition, instance)
        for condition in CONDITIONS
        for instance in expected_instances
    }

    main_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in read_rows(args.main):
        main_by_key[(row["condition"], row["instance_id"])].append(row)
    retry_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in args.retry:
        for row in read_rows(path):
            retry_by_key[(row["condition"], row["instance_id"])].append(row)

    selected = []
    missing = []
    ambiguous = []
    for key in sorted(required):
        valid_main = [row for row in main_by_key[key] if is_valid(row)]
        if len(valid_main) > 1:
            ambiguous.append((key, "main", len(valid_main)))
            continue
        if valid_main:
            row = dict(valid_main[0])
            row["selection_source"] = "main"
            selected.append(row)
            continue
        valid_retries = [row for row in retry_by_key[key] if is_valid(row)]
        if len(valid_retries) != 1:
            if valid_retries:
                ambiguous.append((key, "retry", len(valid_retries)))
            else:
                missing.append(key)
            continue
        row = dict(valid_retries[0])
        row["selection_source"] = "retry"
        selected.append(row)

    foreign = sorted((set(main_by_key) | set(retry_by_key)) - required)
    if foreign or missing or ambiguous:
        raise SystemExit(
            "selection failed: "
            f"selected={len(selected)} missing={missing[:10]} "
            f"ambiguous={ambiguous[:10]} foreign={foreign[:10]}"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in selected:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(args.output), "selected": len(selected)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
