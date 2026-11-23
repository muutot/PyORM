# Maintenance workflow

1. Inspect the exact source, tests, and configuration in scope. Use `rg` for discovery and exclude
   `.venv*`, `.tools`, build artifacts, and caches. Read applicable scoped AGENTS files.
2. Inspect `git status --short --branch` and relevant history. The initial branch is `main`;
   `origin` is `https://github.com/muutot/PyORM.git`. Preserve unrelated changes. Resolve comparison
   baselines before using them; an initialized repository may not yet have commits. Do not run
   missing setup wizards or invent remote release workflows.
3. Use the session requirements as the specification. Check an existing TODO only when its entire
   claim is supported by source and verified behavior.
4. Implement the smallest change that fulfills the request. Preserve the cache, incremental writes,
   failure visibility, and synchronization contracts in the architecture reference.
5. Run proportionate checks from the verification reference. Report skipped integration checks and
   their reason. A mock result is not evidence of server behavior or production performance.
6. Update documentation when public usage or stable behavior changes. Before an authorized commit,
   inspect the actual staged diff and decide whether this skill or its references need updating.

The already agreed verification boundaries are startup/load, ordinary attribute assignment and
embedded defaults, cache identity, container tracking, versioned writes/retries, automatic scheduling,
shutdown, and editor type inference. The imported TDD skill's seam-confirmation rule is satisfied for
these boundaries by this session. Do not ask for the same approval again. Use public behavior; inspect
actual MongoDB writes where the claim specifically concerns persisted data or incremental operators.

For review without Git, compare the supplied files or changes against the session specification.
Keep standards and requirements findings distinguishable. Missing Git or an issue tracker does not
require installing the upstream setup wizard. Delegate only when the user or an invoked, applicable
workflow requests it, and keep edit ownership non-overlapping.
