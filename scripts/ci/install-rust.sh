#!/usr/bin/env bash
set -euo pipefail

# cibuildwheel's manylinux container does not inherit the host's Rust installation.
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs |
    sh -s -- -y --profile minimal
