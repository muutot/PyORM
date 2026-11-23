---
name: pyorm-dev
description: Develop and maintain PyORM, a Python 3.13 MongoDB ORM with an authoritative object cache and automatic incremental persistence. Use for fields, embedded documents, containers, typing, manager scheduling, concurrency, serialization, queries, examples, and project verification.
---

# PyORM development

Read [maintenance-workflow.md](references/maintenance-workflow.md) before changing the project.
Read [architecture.md](references/architecture.md) before changing ORM behavior, types, or examples.

Use the current source, tests, and session requirements as evidence. Keep changes minimal and preserve
unrelated edits. The project instructions and direct user instructions override imported skills.

| Work | Additional reference |
| --- | --- |
| Dependencies, checks, packaging | [verification.md](references/verification.md) |
| Requested audit | [local auditor](../code-auditor/SKILL.md) and its PyORM profile |
| Requested version bump or release | [Python release workflow](../version-release/SKILL.md) |
| Skill provenance and compatibility | [import guide](../../../docs/agents/skill-imports.md) |

Before a commit, inspect the diff and update affected references, README, and examples when contracts,
ownership, defaults, retry behavior, or verification commands change. Record TODO completion only from
implementation evidence and proportionate verification. Do not create TODOs merely to track routine work.
When Git is available and a commit is authorized, commit one verified unit using
`<emoji> <type>[<scope>]: <English imperative message>`: `✨ feat`, `🐛 fix`, `📝 docs`,
`🎨 style`, `♻️ refactor`, `✅ test`, `⚡️ perf`, `🔧 chore`, or `🚀 release`.

Use relevant imported skills by task fit. Skill maintenance does not start an audit or release.
Do not require repeated approval for already agreed interfaces or routine verification.
