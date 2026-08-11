# In-session 3-try Skill Creator (non-canonical)

This diagnostic method gives instance 1 up to three verifier-backed solve
attempts in one explicit Claude Code session.  It stops on the first pass.  A
final turn in the same session uses the injected Skill Creator to write frozen
skills for held-out instances.  Hidden verifier sources are never mounted into
the agent container; only bounded pass/fail feedback is returned.

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
