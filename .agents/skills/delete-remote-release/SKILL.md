---
name: delete-remote-release
description: >
  Delete a remote git tag and/or its GitHub release draft — for example cleaning up a
  failed CI draft release left behind after re-tagging a version, or removing an
  orphaned/duplicate release tag. This skill should be used when the user asks to remove
  a remote (published) git tag, delete a GitHub release draft, or undo a botched release
  tag — e.g. "delete the remote v1.4.0 tag and its draft release", "remove the orphaned
  release draft", "clean up the tag after re-tagging". GitHub only.
---

# delete-remote-release

## Purpose

Remove a remote git tag and/or its GitHub **draft** release for a given version. A botched
or re-tagged release often leaves a stale `vX.Y.Z` draft Release on GitHub that blocks the
next release workflow run ("release already exists"). This skill deletes both the tag and the
draft cleanly so the release workflow can be re-triggered.

Key fact: **deleting a git tag does NOT delete the GitHub Release object.** Both must be
removed, which is exactly what this skill does.

## When to use

- The user asks to delete a remote/remote git tag (e.g. `v1.4.0`).
- The user mentions a "draft release", "orphaned release", or a failed CI run that left a
  draft behind.
- When explicitly asked to clean up an identified failed release in a configured GitHub repository.

PyORM uses `origin` at `https://github.com/muutot/PyORM.git`. Inspect actual remote tags/releases for
explicitly requested cleanup. Importing this skill or configuring Git does not authorize deletion.

## Prerequisites

1. **git push access** to the target remote (for tag deletion). Uses the user's normal git
   credentials — no extra setup.
2. **A GitHub token** for the release-draft API delete. The script resolves it in this
   order: `$GITHUB_TOKEN` env → `gh auth token`. If none is found, tag deletion still runs; the
   draft delete is skipped with instructions. A fine-grained or classic PAT with
   `repo` (or `public_repo`) scope is sufficient.

## How to use

Run the bundled script from the repository root. **By default it is dry-run** — it shows
what would be deleted and changes nothing.

```powershell
# 1) See what would be deleted (safe, no changes):
.\.venv\Scripts\python.exe .agents/skills/delete-remote-release/scripts/delete_remote_release.py --tag v1.4.0

# 2) Actually delete (IRREVERSIBLE):
.\.venv\Scripts\python.exe .agents/skills/delete-remote-release/scripts/delete_remote_release.py --tag v1.4.0 --yes
```

Parameters:

- `--tag <tag>` (required): the tag to clean up, e.g. `v1.4.0`. Never guessed.
- `--remote <name>`: git remote to delete the tag from. Default: `github` if present,
  else `origin`.
- `--repo <owner/repo>`: GitHub repo for the API call. Default: derived from the remote URL.
- `--tag-only`: delete only the remote tag, skip the draft release.
- `--draft-only`: delete only the GitHub draft release, skip the tag.
- `--dry-run`: force dry-run (no changes) even if `--yes` is given.
- `--yes`: perform the actual deletions. Required for any change.

## Safety rules (enforce these)

- **Always run dry-run first** and show the user exactly what will be deleted before
  passing `--yes`.
- **Require an explicit `--tag`** — never infer or default a tag name.
- Deleting a remote tag and a draft release is **not reversible** remotely. Require explicit user
  authorization for the identified target before `--yes`; existing explicit session authorization
  satisfies this requirement. Finish the dry-run preview before asking for any missing authorization.
- Only **draft** releases matching the tag are auto-deleted. If a **published** (non-draft)
  release shares the tag, the script warns and leaves it alone — deleting a published
  release is a bigger deal and must be a conscious, separate decision.
- Published releases are never auto-deleted. If the user wants a published release removed,
  confirm explicitly and act deliberately.

## After cleanup

Report the actual deleted or retained targets. A version rebuild does not authorize deleting tags,
rewriting history, force-pushing, or triggering a remote release. Follow the user's requested repository
workflow only after its concrete configuration and authorization are established.
