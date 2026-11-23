"""Check an installed wheel, then run both backends outside the source checkout."""

import argparse
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("python", "rust"), required=True)
    args = parser.parse_args()
    os.environ["PYORM_NATIVE"] = "required" if args.backend == "rust" else "off"

    import pyorm
    from pyorm._acceleration import backend_name, native

    package = Path(pyorm.__file__).resolve().parent
    if package.is_relative_to(Path.cwd().resolve() / "pyorm"):
        raise RuntimeError(f"Tests imported the checkout instead of the wheel: {package}")
    if backend_name != args.backend or not (package / "py.typed").is_file():
        raise RuntimeError("Installed wheel is missing its requested backend or py.typed")
    if native is not None and Path(native.__file__).resolve().parent != package:
        raise RuntimeError("Native extension was loaded from a different package")
    print(
        f"Installed pyorm-mongo {importlib.metadata.version('pyorm-mongo')}: "
        f"{backend_name}, {package}",
        flush=True,
    )
    modes = ("required", "off") if args.backend == "rust" else ("off",)
    for mode in modes:
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests"],
            env={**os.environ, "PYORM_NATIVE": mode},
            check=True,
        )


if __name__ == "__main__":
    main()
