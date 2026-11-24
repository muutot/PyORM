# Architecture and contracts

PyORM supports Python 3.13 and synchronous PyMongo. A shared MongoClient owns the connection pool;
worker threads do not create a separate client per operation.

| Module | Responsibility |
| --- | --- |
| `pyorm/runtime.py` | Startup singleton `init`, `get_manager`, lifecycle `shutdown` |
| `pyorm/manager.py` | Identity cache, dirty generations, automatic scheduling, persistence retries |
| `pyorm/model.py` | Model metaclass, field registration, embedded state, business method synchronization |
| `pyorm/fields.py` | Typed descriptors, defaults, validation, BSON restoration |
| `pyorm/tracking.py` | Nested change hooks and tracked containers |
| `pyorm/serialization.py` | BSON encoding and incremental document differences |
| `pyorm/_acceleration.py`, `native/` | Optional Rust BSON helpers with Python fallback |
| `pyorm/connection.py`, `query.py`, `executor.py` | Lower-level repositories, queries, ordered execution |

Initialize the manager once before business code. Examples use `Model.load(id)` and `self.attr = value`;
the manager schedules persistence when dirty-document count reaches `batch_size` or the flush interval
expires. `shutdown()` drains persistence at the application boundary. Business methods need no manual
submit/flush/reload calls.

Within one Manager lifetime, each model/id has one cached instance, loaded in full on first access.
Later writes update changed paths. Acknowledgements must never overwrite newer cache values. Failed
writes retain pending state; shutdown failures remain visible. This ownership contract assumes writers
use the same manager; it does not establish cross-process cache coherence or track external DB edits.

Ordinary synchronous instance methods are automatically synchronized using the root document RLock.
Individual tracked mutations also acquire the lock. Multiple top-level statements, operations across
documents, asynchronous methods, and generators are not automatically one atomic business transaction.
Embedded documents share their root lock after binding and cannot belong to two managed fields.

`_autosave` turns listed ordinary class defaults into fields, including on EmbeddedDocument. Every
explicit Field subclass auto-registers without an `_autosave` entry. `EmbeddedField(PetStats)` infers
`PetStats` and defaults to a fresh `PetStats()` per owner. Explicit `default` or `default_factory`
overrides this behavior; use a factory for constructors that need arguments. Class descriptor access
retains EmbeddedField; instance access yields the declared type. `py.typed` ships in the wheel.

Autosave annotations declare runtime top-level types, including optional/union and concrete embedded
types; otherwise infer builtin scalar/container types or the embedded/enum class from defaults.
Unannotated defaults retain nullable=True; None/missing stays dynamic. Numeric types reject bool unless
explicitly allowed; float does not coerce/accept int. Explicit Field parameters remain authoritative.
Postponed/local annotations resolve at class declaration, not on mutations. Element generics remain
editor hints unless an item Field is supplied. Embedded template deepcopy copies field/extra state
with fresh locks and no ownership hooks, preserving Python defaults without running factories or
constructors. Tracked containers deepcopy to independent builtin values; root Documents reject deepcopy.
Union annotations involving set/frozenset/tuple/Enum restoration require one non-null concrete type;
mixed non-null targets require a custom Field codec and fail at class declaration.
The stricter scalar behavior requires legacy-data migration; see
[field validation](../../../../docs/field-validation.md).

`Document[ID]` declares identifier types (ObjectId by default). Model class methods use Self;
Manager, Repository, QuerySet, and Future preserve concrete models. String-ID examples use Document[str].
Use meaningful method types for editor assistance. Leave dynamic keyword forwarding unannotated rather
than padding it with Any/object; avoid runtime cast calls and type-driven conversions. Heterogeneous
registries and raw BSON remain dynamic boundaries internally.

Saved list/dict/tuple/set defaults restore their top-level Python container types. Dict edits use dotted
paths. One new array tail element uses an indexed `$set`; multiple additions use `$push/$each`. Set
additions are deduplicated in cache. Other array changes and removals can require whole-array replacement.
Version-filtered retries must not replay acknowledged `$push` additions: confirm a lost acknowledgement
using the write receipt without reloading business data. Do not claim database cost from payload size
alone; use real server measurements under the relevant workload.

Capture detaches BSON values before deriving operators. The packet owns both its values and operator
payloads; each database attempt receives a detached operator copy. On acknowledgement, the retired
packet transfers its values to the snapshot without copying the complete tree again. Native scalar
fast paths use exact types; driver values and custom subclasses retain deepcopy behavior. Repeated
mutations keep the first flush deadline and wake scheduling only for new pending work, unblocking,
or changed batch/interval settings. Write completion wakes scheduling for newer generations.

The dispatcher selects only available capacity during a forced drain, and stops counting eligible
work once the batch threshold is met. Pending state, queue order, retries, deadlines, and failure
visibility stay authoritative in the existing Python manager; blocked and future retries are skipped.

Optional Rust helpers accelerate exact BSON containers, copying, set ordering/membership, and array
prefix comparisons. Custom conversions, subclasses, driver values, and depth beyond 32 retain Python
fallback behavior. Dictionary/list traversal uses Python iteration semantics around callbacks. Native
copies preserve aliases/cycles and callback order; byte comparisons retain BSON type and field order.
Safe PyO3 code releases the GIL only over immutable BSON bytes and Rust-owned metadata. Rust does not
own locks, cache identity, generations, write packets, or persistence. Runtime PYORM_NATIVE selects
auto/off/required at import; PYORM_BUILD_NATIVE selects optional/disabled/mandatory source builds.

Treat the current implementations and tests as authoritative; update this map when ownership changes.
