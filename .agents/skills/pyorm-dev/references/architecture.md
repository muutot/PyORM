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

Treat the current implementations and tests as authoritative; update this map when ownership changes.
