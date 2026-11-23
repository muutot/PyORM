# Native acceleration

Keep Rust restricted to BSON traversal, detached copying, and byte comparisons. Manager identity,
locks, generations, scheduling, write receipts, and retries remain owned by Python.

Use safe PyO3 APIs; do not introduce unsafe blocks or reinterpret Python object layouts. Release
the GIL only while working with borrowed immutable BSON bytes and Rust-owned metadata. Callbacks
and Python containers require the GIL. Limit native recursion and preserve the Python fallback for
custom types, validators, BSON driver values, and conversion failures.

Verify native and forced-Python modes separately, including real MongoDB persistence. Differential
tests must include nested values, aliases/cycles in copies, typed BSON equality, set ordering,
malformed bytes, and retry/concurrency behavior. Measure complete capture/drain workloads rather
than inferring gains from isolated Rust loops. Keep optional native builds and pure-Python wheels usable.
