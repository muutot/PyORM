"""Build an optional Rust accelerator while keeping source installs portable."""

import os

from setuptools import setup
from setuptools_rust import Binding, RustExtension

mode = os.environ.get("PYORM_BUILD_NATIVE", "auto")
if mode not in ("auto", "off", "required"):
    raise ValueError("PYORM_BUILD_NATIVE must be auto, off, or required")

setup(
    rust_extensions=(
        []
        if mode == "off"
        else [
            RustExtension(
                "pyorm._native",
                path="native/Cargo.toml",
                binding=Binding.PyO3,
                args=["--locked"],
                optional=mode == "auto",
                debug=False,
            )
        ]
    ),
    zip_safe=False,
)
