---
name: skill-creator
description: Create reusable Claude Code skills with precise trigger metadata and compact operational instructions. Use when asked to capture a solved or attempted workflow as a SKILL.md.
---

# Skill Creator

Extract reusable knowledge from the current session rather than copying the
particular answer.  Write each skill under
`environment/skills/<skill-name>/SKILL.md`.

Each file must start with YAML frontmatter containing `name` and
`description`.  Use a lowercase hyphenated name.  Make the description state
both what the skill does and when it should trigger.  Keep instructions
operational, concise, and general enough to apply to sibling task instances.

Do not include hidden-test guesses, task-specific final outputs, credentials,
or absolute paths that only exist in the current instance.  Prefer one focused
skill; create additional skills only for genuinely separate reusable topics.

