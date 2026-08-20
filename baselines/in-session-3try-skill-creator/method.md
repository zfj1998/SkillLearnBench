# In-session 3-try Skill Creator (non-canonical)

This family method gives instance 1 up to three verifier-backed solve
attempts in one explicit Claude Code session.  It stops on the first pass.  A
final turn in the same session uses the injected Skill Creator to write frozen
skills. Instances 2-5 each consume the frozen library in a fresh environment
and fresh session with one attempt. Hidden verifier sources are never mounted
into an agent container; only bounded pass/fail feedback is returned.

If reflection completes but produces no valid skill, that is retained as model
behavior and instances 2-5 still run with an empty frozen library. A reflection
runtime failure, missing evaluator evidence, continuity failure, held-out
coverage/session mismatch, or mutation after freeze remains an unscoreable
protocol failure.

After every solve attempt and the terminal reflection, the method exports the
native Claude Code session JSONL. It fails closed unless each later export is a
strict non-empty byte-prefix extension of the prior export, every main-chain
message has the immediately preceding parent UUID, the exact phase prompt is
present once in the appended suffix, and every tool result links to an earlier
tool call. These snapshots are retained as protocol evidence.

The downloaded trial can be independently checked with:

```bash
python ap/audit_selfgen_continuity.py /path/to/trial-directory
```

## Weak-learning ablations

`SELFGEN_ABLATION_MODE` selects one of four audited protocols while keeping the
same frozen-library held-out evaluation:

- `standard`: the protocol above.
- `prompt-only`: give Skill Creator only the instance-1 instruction; do not run
  the learning environment or verifier. The generation turn is restricted to
  `Skill`, `Write`, and non-environment task bookkeeping tools.
- `family-only`: give Skill Creator only the family identifier and a plain-text
  rendering of that identifier; do not expose an instance prompt, environment,
  solve trajectory, or verifier feedback. The generation turn is restricted to
  `Skill`, `Write`, and non-environment task bookkeeping tools.
- `trajectory-summary`: retain the same-session verifier-backed solve attempts,
  but replace Skill Creator with an ordinary summary turn restricted to
  `Write` plus non-environment task bookkeeping tools.

Every artifact records the selected mode, protocol version, whether the
learning environment and verifier ran, the generation allowlist, and the tools
actually used. The offline auditor fails closed when those claims disagree
with the retained trajectories.
