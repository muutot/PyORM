"""Validate release metadata and the complete wheel matrix before publishing."""

import argparse
import hashlib
import os
import re
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def project_version() -> str:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    if project["name"] != "pyorm-mongo":
        raise ValueError("Expected the pyorm-mongo project")
    version = project["version"]
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        raise ValueError("Package version must use numeric x.y.z format")
    return version


def check_metadata(version: str) -> None:
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME")
        if tag != f"v{version}":
            raise ValueError(f"Tag {tag!r} does not match package version v{version}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"version={version}\n")
    print(f"Package version: {version}")


def check_artifacts(directory: Path, version: str) -> None:
    expected_native = {
        (python, platform)
        for python in ("cp313", "cp314")
        for platform in ("win_amd64", "manylinux_x86_64", "macosx_arm64")
    }
    seen_native = set()
    wheels = sorted(directory.glob("*.whl"))
    pure_count = 0
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            metadata = BytesParser().parsebytes(
                archive.read(f"pyorm_mongo-{version}.dist-info/METADATA")
            )
            if metadata["Name"] != "pyorm-mongo" or metadata["Version"] != version:
                raise ValueError(f"Incorrect package metadata: {wheel.name}")
            if metadata["Requires-Python"] != ">=3.13":
                raise ValueError(f"Incorrect Python requirement: {wheel.name}")
            if "pyorm/py.typed" not in archive.namelist():
                raise ValueError(f"Missing py.typed: {wheel.name}")
            extensions = [
                name
                for name in archive.namelist()
                if name.startswith("pyorm/_native.") and name.endswith((".so", ".pyd"))
            ]
        if wheel.name == f"pyorm_mongo-{version}-py3-none-any.whl":
            if extensions:
                raise ValueError("Pure Python wheel contains a native extension")
            pure_count += 1
            continue
        match = re.fullmatch(
            rf"pyorm_mongo-{re.escape(version)}-(cp313|cp314)-\1-(.+)\.whl", wheel.name
        )
        if match is None or len(extensions) != 1:
            raise ValueError(f"Unexpected native wheel or missing extension: {wheel.name}")
        python, tag = match.groups()
        if tag == "win_amd64":
            platform = tag
        elif re.fullmatch(r"manylinux_2_28_x86_64(?:\.[\w]+)*", tag):
            platform = "manylinux_x86_64"
        elif re.fullmatch(r"macosx_\d+_\d+_arm64", tag):
            platform = "macosx_arm64"
        else:
            raise ValueError(f"Unexpected platform: {wheel.name}")
        key = (python, platform)
        if key in seen_native:
            raise ValueError(f"Duplicate native wheel: {wheel.name}")
        seen_native.add(key)
    if pure_count != 1 or seen_native != expected_native:
        raise ValueError(f"Incomplete wheel matrix: pure={pure_count}, native={seen_native}")
    sources = sorted(directory.glob("*.tar.gz"))
    if len(sources) != 1 or sources[0].name != f"pyorm_mongo-{version}.tar.gz":
        raise ValueError("Expected exactly one matching source archive")
    with tarfile.open(sources[0]) as archive:
        prefix = f"pyorm_mongo-{version}/"
        for name in (
            "PKG-INFO",
            "pyproject.toml",
            "setup.py",
            "pyorm/__init__.py",
            "pyorm/py.typed",
            "native/Cargo.toml",
            "native/Cargo.lock",
            "native/src/lib.rs",
        ):
            member = archive.extractfile(prefix + name)
            if member is None:
                raise ValueError(f"Missing source file: {name}")
            if name == "PKG-INFO":
                metadata = BytesParser().parsebytes(member.read())
                if metadata["Name"] != "pyorm-mongo" or metadata["Version"] != version:
                    raise ValueError("Incorrect source archive metadata")
    files = sorted([*wheels, *sources])
    unexpected = set(directory.iterdir()) - set(files) - {directory / "SHA256SUMS"}
    if unexpected:
        raise ValueError(f"Unexpected release files: {unexpected}")
    checksums = []
    for path in files:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{digest}  {path.name}\n")
    (directory / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")
    print(f"Verified {len(wheels)} wheels and one source archive; wrote SHA256SUMS")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("metadata", "artifacts"))
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    args = parser.parse_args()
    version = project_version()
    if args.command == "metadata":
        check_metadata(version)
    else:
        check_artifacts(args.directory, version)


if __name__ == "__main__":
    main()
