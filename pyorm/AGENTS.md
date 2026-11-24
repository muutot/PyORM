# ORM source

Read the pyorm-dev architecture reference before behavior changes. Keep a shared MongoClient pool,
one active cached model/id, generation-aware acknowledgements, and incremental writes. Explicit
release/delete invalidates old references and serializes concurrent gets; collection/model registration
lasts for the Manager lifetime. Do not reload business data after acknowledgements or silently discard
failed writes. Explicit discard/delete waits for uncertain packets before retiring. Keep root lock
ownership consistent for nested values and do not hold manager scheduling locks across network I/O.

All explicit Field subclasses auto-register; `_autosave` infers top-level types from annotations/defaults.
Preserve numeric bool exclusion and legacy unannotated nullability. Preserve independent
defaults, BSON validation, unknown-field retention, and top-level container restoration. Typed descriptor
class access returns the descriptor; instance access returns the declared value type. Verify typing
changes with Pyright and wheel py.typed checks, plus runtime checks when defaults or behavior change.
