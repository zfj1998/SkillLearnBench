# In-session 3-try Skill Creator (non-canonical)

This diagnostic method gives instance 1 up to three verifier-backed solve
attempts in one explicit Claude Code session.  It stops on the first pass.  A
final turn in the same session uses the injected Skill Creator to write frozen
skills for held-out instances.  Hidden verifier sources are never mounted into
the agent container; only bounded pass/fail feedback is returned.

