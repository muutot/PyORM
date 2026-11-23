# Tests

Verify behavior at the agreed interfaces in root AGENTS. For persisted data/operator claims, inspect
independent database state or actual updates; do not use a result derived from the implementation as
the expected value. Preserve lost-acknowledgement, newer-cache, and concurrency coverage.

Use bounded Events/timeouts for concurrency checks. Real MongoDB tests use `PYORM_TEST_URI` and disposable
`pyorm_test_*` databases only. Report missing real-server coverage explicitly. Keep typing assertions in
`tests/typing`, checked by Pyright; pytest does not execute these fixtures as runtime tests.
