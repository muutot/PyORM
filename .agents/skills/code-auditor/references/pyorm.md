# PyORM audit profile

Use the current session and README as the requirements. Read
[architecture.md](../../pyorm-dev/references/architecture.md) and
[verification.md](../../pyorm-dev/references/verification.md) for ownership and checks.

Prioritize cache identity and memory ordering, loss of dirty changes during acknowledgements, nested
mutation tracking, retry idempotency (especially `$push`), version conflicts, queue backpressure,
deadlocks, pool sharing, shutdown error visibility, BSON validation, unknown-field preservation,
container restoration, and descriptor type inference. Check explicit field auto-registration and
independent embedded defaults. Inspect actual lock/condition ownership rather than treating the GIL
as a concurrency guarantee.

For performance, separate CPU diff/tracking cost, network payload, connection contention, and database
write/index cost. A smaller MongoDB operator payload does not by itself prove lower database cost.
Use real MongoDB for operator semantics and retry tests; mongomock is useful for fast behavior checks.

Python 3.13, PyMongo, uv, setuptools, Ruff, Pyright, and pytest are the project toolchain. The initial
Git branch is main and origin is https://github.com/muutot/PyORM.git. Resolve the requested baseline
before incremental comparisons; an initialized repository may not yet have commits. Frontend,
accessibility, mobile, and LLM execution are not established
by the presence of imported audit prompts; mark absent surfaces Not assessed with a reason.

Inspect runtime sources separately from the imported agent skill bundle. Exclude .venv*, .tools,
build/dist, egg-info, and caches. Audit reports are local outputs under this skill's result directory;
do not include credentials, private source-project data, or unrelated user files in findings.
