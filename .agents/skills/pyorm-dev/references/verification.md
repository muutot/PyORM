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

For real MongoDB, set `PYORM_TEST_URI` to a disposable server and run pytest. Integration fixtures use
random `pyorm_test_*` databases and drop only their own databases. If starting a temporary mongod, bind
to loopback, keep its data in the workspace, wait for readiness, and stop it in `finally`. Keep benchmarks
separate from correctness verification and report environment and workload with performance claims.

Inspect the built wheel for package metadata, `pyorm/py.typed`, and successful isolated imports/type
checks. If Windows denies overwriting old build metadata, stage the package sources, pyproject.toml,
README, and py.typed in a fresh workspace subdirectory and use uv build there. Retain existing artifacts.
