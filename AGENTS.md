# PyORM repository instructions

Before project changes, read `.agents/skills/pyorm-dev/SKILL.md` and its maintenance workflow.
Use its architecture and verification references for the subsystem in scope. Direct user instructions
and this file override imported skills.

- Target Python 3.13. Use uv for environments and package installation; explicitly select this
  project's `.venv/Scripts/python.exe`. Use copy linking when uv cache and environment are on different drives.
- Prioritize useful method signatures, concrete model/ID generics, and editor inference. Do not add
  meaningless `: Any`/`: object` parameter annotations or runtime cast calls to satisfy a checker.
  Static annotation work must not add hot-path conversions or runtime type checks.
- Preserve manager-owned cache identity, automatic scheduling, incremental persistence, retry safety,
  internal synchronization, and visible failures. Business examples use startup init and attributes.
- Check TODO completion from direct evidence. Update affected docs and references when contracts change;
  inspect documentation currency before an authorized commit. Preserve unrelated user edits.
- This Git repository starts on `main` and uses `origin` at `https://github.com/muutot/PyORM.git`.
  Use one verified unit and the pyorm-dev gitmoji convention for authorized commits. Inspect actual
  branches, remote state, and workflows before release operations; do not invent release automation.
- Use the local `.agents/skills/code-auditor` copy and PyORM profile for requested audits. Use the local
  Python `version-release` workflow for requested version work. Importing skills triggers neither.
- Six shared skills are vendored as documented in `docs/agents/skill-imports.md`. Already agreed test
  boundaries are startup/load, attribute and embedded assignment/defaults, cache identity, containers,
  persistence/retries/scheduling, shutdown, and typing; do not request their approval again.
- For review without Git/spec files, use supplied comparisons and session requirements. Do not require
  the upstream setup wizard or an invented issue tracker. Use delegation only when the user or an
  invoked applicable workflow asks for it; keep parallel edit ownership non-overlapping.

Scoped instructions under pyorm, tests, and examples further describe their contracts.
