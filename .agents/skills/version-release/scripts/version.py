"""Inspect or apply a requested numeric PyORM version bump; no remote operations."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


def numeric_version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise ValueError("Version must use numeric x.y.z format")
    major, minor, patch = map(int, value.split("."))
    return major, minor, patch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--version", help="Requested numeric x.y.z version")
    parser.add_argument("--apply", action="store_true", help="Apply the proposed version bump")
    args = parser.parse_args()
    try:
        manifest = args.root.resolve() / "pyproject.toml"
        raw = manifest.read_bytes()
        text = raw.decode("utf-8")
        project = tomllib.loads(text)["project"]
        if project["name"] != "pyorm-mongo":
            raise ValueError("Expected the pyorm-mongo project")
        current = project["version"]
        target = args.version or current
        current_parts, target_parts = numeric_version(current), numeric_version(target)
        if args.apply and not args.version:
            raise ValueError("--apply requires an explicit --version")
        if args.version and target_parts <= current_parts:
            raise ValueError("Requested version must be greater than the current version")
        section = re.search(r"(?ms)^\[project\][ \t]*\r?\n(.*?)(?=^\[|\Z)", text)
        if section is None:
            raise ValueError("Missing [project] section")
        body, count = re.subn(
            r"(?m)^(version[ \t]*=[ \t]*[\'\"])[^\'\"\r\n]+([\'\"])",
            lambda match: match[1] + target + match[2],
            section[1],
        )
        if count != 1:
            raise ValueError("Expected one plain project version assignment")
        updated = text[: section.start(1)] + body + text[section.end(1) :]
        if tomllib.loads(updated)["project"]["version"] != target:
            raise ValueError("Updated metadata did not match the requested version")
        if args.apply:
            if manifest.read_bytes() != raw:
                raise ValueError("Manifest changed during version inspection; retry")
            manifest.write_bytes(updated.encode("utf-8"))
        print(json.dumps({"current": current, "target": target, "applied": args.apply}))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Version check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
