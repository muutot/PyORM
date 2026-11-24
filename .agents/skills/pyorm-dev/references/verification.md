# Verification and tooling

Run from the project root in PowerShell. Always select this project's Python 3.13 environment.
Use uv for environment and package operations. On Windows, use copy linking across drives.

```powershell
uv venv --python 3.13 .venv
uv pip install --python .\.venv\Scripts\python.exe --link-mode copy -e '.[dev]'
.\.venv\Scripts\python.exe -m ruff format --check pyorm tests examples
.\.venv\Scripts\python.exe -m ruff check pyorm tests examples
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\python.exe -m pytest -q
uv build --python .\.venv\Scripts\python.exe --link-mode copy --wheel
```

Do not recreate a working environment. Add packages with uv only when needed. Run meaningful tests for
changed behavior; documentation changes require link/command checks rather than new ORM unit tests.
Pyright checks `tests/typing` and the quickstart for descriptor inference. Ruff excludes vendored skills;
check newly owned Python helper scripts explicitly. Do not reformat unmodified imported shared skills.

The GitHub [release workflow](../../../../.github/workflows/release.yml) checks scripts/ci alongside
the package, tests and examples. It builds pure and CPython 3.13/3.14 native wheels, validates installed
backends outside the checkout, and gates tag releases on real MongoDB checks. See
[release instructions](../../../../docs/releases.md) for the supported platforms and trigger behavior.

For real MongoDB, set `PYORM_TEST_URI` to a disposable server and run pytest. Integration fixtures use
random `pyorm_test_*` databases and drop only their own databases. If starting a temporary mongod, bind
to loopback, keep its data in the workspace, wait for readiness, and stop it in `finally`. Keep benchmarks
separate from correctness verification and report environment and workload with performance claims.
`examples/benchmark_hot_paths.py --rounds 5 --output docs/benchmarks/performance-current.json` measures cache work,
packet capture, and acknowledged MongoDB drains separately; capture timing is not server performance.
Use the same isolated service and workload for comparisons, alternate versions, and retain all samples.

Scalar/default regressions live in `tests/test_autosave_validation.py`; annotation signatures
are also checked in `tests/typing`.

For native changes, run the complete suite with PYORM_NATIVE=required and off, including real MongoDB.
Verify differential BSON ordering/type comparisons, alias/cycle copying, callback failures/mutations,
deep nesting, malformed buffers, retries, and concurrency. With PYO3_PYTHON set to the resolved project
interpreter, run cargo fmt --check, cargo clippy --all-targets -- -D warnings, and cargo test using
--manifest-path native/Cargo.toml. Native recursion is bounded; helpers must remain safe Rust.

Build native and pure wheels with PYORM_BUILD_NATIVE=required and off. Test an auto source build without
a Rust compiler, required-mode failure, and sdist rebuilding with Cargo.lock. Inspect isolated installs,
backend selection, py.typed, and model inference. Runtime auto handles an absent module; unrelated missing
dependencies and required-mode load failures remain visible. No cross-platform native validation is
implied by a Windows build. --max-pending controls benchmark queue capacity; output records backend and
implementation_path. Tracemalloc excludes native Rust allocations and is not process memory evidence.
Source-archive rebuilds use a fresh Cargo target directory: restored timestamps can otherwise reuse
another snapshot's binary. Verify installed native binary bytes against the wheel and run behavior
tests from installed wheels with the project's editable import hook disabled, so an absent extension
cannot be supplied by the source checkout. Keep wheel tests connected to the disposable MongoDB server.

Inspect the built wheel for package metadata, `pyorm/py.typed`, and successful isolated imports/type
checks. If Windows denies overwriting old build metadata, stage the package sources, pyproject.toml,
README, and py.typed in a fresh workspace subdirectory and use uv build there. Retain existing artifacts.
