---
name: version-release
description: Prepare a PyORM Python package version bump or release when explicitly requested. Uses pyproject.toml as the version source, uv with Python 3.13, behavior and typing checks, and wheel inspection. Supports rebuilding the current version without inventing Git or remote release configuration.
---

# PyORM version and release

Read [verification.md](../pyorm-dev/references/verification.md). Use `pyproject.toml` as the single
version source. Git starts on `main`, with `origin` at `https://github.com/muutot/PyORM.git`.
Inspect actual history and remote state for requested release work. The configured GitHub Release
workflow is [release.yml](../../../.github/workflows/release.yml); its triggers, wheel matrix and
verification gates are documented in [releases.md](../../../docs/releases.md). Do not assume
package-index publishing, or rewrite history/delete tags/force-push as a side effect of preparing a package.

1. Establish the requested version and scope from the user instruction. Inspect the final source and
   any existing changelog. Do not bump the version for routine development or skill installation.
2. Run the bundled helper without `--apply` to inspect metadata and the proposed version:

   ```powershell
   .\.venv\Scripts\python.exe .agents/skills/version-release/scripts/version.py --version 0.2.4
   ```

   With no `--version`, it checks and reports the current version. It supports numeric `x.y.z` bumps.
   Apply a requested bump with the same arguments plus `--apply`. Preserve unrelated metadata.
3. Update release notes when the user requests them, describing final behavior and validation rather
   than conversational history. Inspect README and development references for currency.
4. Validate the final tree with Ruff, Pyright, and appropriate pytest checks. Include real MongoDB
   verification for persistence/operator changes before claiming release readiness.
5. GitHub Release artifacts must all be built by the configured Actions workflow from the release
   tag, with native builds required and all verification gates passing. Do not upload local packages
   to that Release. For an explicitly requested local regeneration, use
   `uv build --python .\.venv\Scripts\python.exe --link-mode copy --wheel`, verify native and pure
   wheels as described in the verification reference, and inspect metadata, package files, py.typed
   and isolated import/type inference. Avoid old Windows metadata overwrite failures using a fresh
   staging folder.

For `--regenerate` requests, rebuild current content at the current version after the same checks;
do not rewrite commits or tags. For authorized commits, keep formatting-only changes separate where
practical and follow the root commit convention. Publishing requires the user's
requested destination and authorization; finish the concrete artifact before requesting missing approval.
