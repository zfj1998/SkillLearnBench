# Self-generated skill family protocol

Protocol identifier: `in-session-3try-skill-creator-family-v2`.

## Evaluation unit

One SkillLearnBench family is one indivisible AP job:

1. Instance 1 is the learning playground. Claude Code receives up to three
   verifier-backed attempts in one session and stops after the first pass.
2. A terminal turn in that same session invokes the injected Skill Creator.
3. The candidate is validated and the accepted library is frozen. A missing or
   invalid candidate is recorded as model behavior and freezes an empty library.
4. Every remaining instance in the family runs once in numeric order in a
   distinct fresh environment and distinct fresh session using the same frozen
   library. Family sizes vary from 2 to 6 instances.

The formal matched denominator is the 80 held-out instances: all 100 benchmark
instances except instance 1 from each of 20 families. Instance 1 outcomes and
attempt counts are diagnostic and are not included in the held-out score.

## Required evidence

- exact learning attempt count and official verifier evidence for each attempt;
- native Claude session snapshots after every attempt and reflection;
- strict byte-prefix, parent-DAG, prompt ancestry, and tool-link continuity;
- reflection candidate, validation outcome, frozen-library manifest and hashes;
- exact ordered held-out IDs discovered from the family and distinct sessions;
- official verifier evidence for every held-out instance;
- identical frozen manifests for every held-out and no post-freeze mutation;
- proof that hidden tests were not mounted into any agent container.

## Failure and scoring semantics

No candidate, an invalid candidate, or an unhelpful valid skill is model
behavior. All held-out instances still execute, and their official
verifier outcomes remain in the denominator.

Missing evaluator evidence, broken required session continuity, reflection
runtime failure, incomplete held-out coverage, reused held-out sessions,
post-freeze mutation, or artifact/provenance failure is an unscoreable protocol
failure. AP retry is infrastructure recovery and is not a learning attempt.

For family gate diagnostics, `scoreable=false`. After a complete family passes
the downloaded-artifact audit, the formal 20-family group can expose:

```text
family held-out score = passed held-out siblings / held-out sibling count
full held-out score   = passed held-out instances / 80
```

All no-skill, human-authored-skill, and model-generated-skill comparisons must
use the same 80 held-out instance IDs.
