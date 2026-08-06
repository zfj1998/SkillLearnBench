# AP phase-1 evaluation

This integration evaluates the pinned SkillLearnBench checkout on Agent
Platform with two matched conditions:

- `no_skill`: an empty `environment/skills/` directory;
- `human_authored`: the task family's committed `skills/human_authored/` tree.

Each of the 100 benchmark instances is one independent Harbor/AP job. The
primary aggregate follows upstream: average verifier rewards within each of
the 20 task families, then average the 20 task-family means. Only the
deterministic task verifier is scored; the GPT-based skill/trajectory metrics
are intentionally outside this phase.

The current immutable dataset is
`skilllearnbench/skilllearnbench-a0da045-phase1-v4`. It pins upstream revision
`a0da045a8bf64b8a8ff20730c4d6ef10dc4e2c5b` and applies one symmetric
infrastructure-only normalization to both conditions: the two Scala task
images install only the requested Scala 2.13.12 toolchain instead of running
the unrelated, rate-limit-prone `cs setup` step.

The upstream learning protocol is separate from evaluation: a method sees only
`<task>/<task>-1` (without tests, human skills, or sibling instances), writes
one task-level skill, and that frozen skill is then evaluated on every instance
of the task family, including instance 1. Phase 1 does not generate a skill.

## Stages

```bash
python ap/build_packages.py        # local reproducibility + package audit
./ap/publish_packages.sh           # AP publisher uses staging-bucket credentials

# Does not submit. REASONING_EFFORT must still be explicit so the preview is
# byte-for-byte representative of the eventual live request.
REASONING_EFFORT=high ./ap/submit_ap.sh dry-run

# One matched instance in both conditions, then the complete 200-job run.
REASONING_EFFORT=high ./ap/submit_ap.sh smoke
REASONING_EFFORT=high ./ap/submit_ap.sh full
```

The scripts default to Claude Code 2.1.220 and Routify `claude-opus-5` over the
native Anthropic endpoint. Credentials are loaded from the workspace `.env` and
are never written to the committed tree. Submission output is redacted.

After downloading results, normalize one selected scientific attempt per
instance to JSONL records containing `instance_id`, `condition`, and `reward`,
then run:

```bash
python ap/audit_results.py ap/artifacts/selected-trials.jsonl \
  --output ap/artifacts/phase1-report.json
```

The audit fails closed on missing, duplicate, foreign, non-binary, or
infrastructure-failed rows.
