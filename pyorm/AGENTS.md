# ORM source

Read the pyorm-dev architecture reference before behavior changes. Keep a shared MongoClient pool,
one cached model/id per Manager lifetime, generation-aware acknowledgements, and incremental writes.
Do not reload business data after acknowledgements or silently discard failed writes. Keep root lock
ownership consistent for nested values and do not hold manager scheduling locks across network I/O.

All explicit Field subclasses auto-register; `_autosave` converts ordinary defaults. Preserve independent
defaults, BSON validation, unknown-field retention, and top-level container restoration. Typed descriptor
class access returns the descriptor; instance access returns the declared value type. Verify typing
changes with Pyright and wheel py.typed checks, plus runtime checks when defaults or behavior change.
