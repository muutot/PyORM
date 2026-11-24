import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import mongomock
import pytest
from pymongo import MongoClient
from pymongo.errors import AutoReconnect, DuplicateKeyError

from pyorm import (
    ConcurrentModificationError,
    ConnectionClosedError,
    Document,
    DocumentInactiveError,
    EmbeddedDocument,
    EmbeddedField,
    Field,
    Manager,
    PersistenceError,
    get_manager,
    init,
    shutdown,
)


class Details(EmbeddedDocument):
    score = Field(int, default=0)


class Entry(Document[int]):
    __collection__ = "entries"
    value = Field(int, default=0)
    items = Field(list, default_factory=list)
    details = EmbeddedField(Details)

    def delete_inside_method(self):
        return self.delete()

    def load_inside_method(self, manager):
        return manager.get(type(self), self.id)

    def edit(self, value):
        self.value = value
        self.items.extend([value, -value])


@pytest.fixture(
    params=["mock", pytest.param("mongo", marks=pytest.mark.integration)],
)
def database(request):
    if request.param == "mock":
        client = mongomock.MongoClient()
    else:
        uri = os.environ.get("PYORM_TEST_URI")
        if not uri:
            pytest.skip("PYORM_TEST_URI is not set")
        client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
    name = "pyorm_test_" + uuid.uuid4().hex
    try:
        yield client[name]
    finally:
        assert name.startswith("pyorm_test_")
        client.drop_database(name)
        client.close()


def seed(database, id=1):
    database.entries.insert_one(
        {
            "_id": id,
            "_orm_version": 1,
            "_orm_write_token": "seed",
            "value": 0,
            "items": [],
            "details": {"score": 0},
        }
    )


def make_manager(database, monkeypatch, **options):
    manager = Manager(
        client=database.client,
        database=database.name,
        flush_interval=60,
        retry_interval=0.01,
        on_error=lambda *_: None,
        **options,
    )
    collection = database.entries
    monkeypatch.setattr(manager._orm, "_collection", lambda _: collection)
    return manager, collection


def wait_retiring(manager):
    with manager._condition:
        assert manager._condition.wait_for(lambda: manager.stats["retiring"] == 1, 5)


def assert_inactive(entry, items, details):
    for operation in (
        lambda: setattr(entry, "value", 99),
        lambda: items.append(99),
        lambda: setattr(details, "score", 99),
        entry.save,
        entry.save_async,
        entry.reload,
    ):
        with pytest.raises(DocumentInactiveError):
            operation()
    assert not entry.is_dirty


def test_managed_document_can_be_deleted(database, monkeypatch):
    manager, collection = make_manager(database, monkeypatch)
    with manager:
        entry = manager.get(Entry, 1)
        entry.value = 1
        manager.wait_idle(5)
        items, details = entry.items, entry.details
        assert entry.delete()
        assert collection.find_one({"_id": 1}) is None
        assert manager.stats["cached"] == 0
        assert not entry.is_persisted and not entry.delete()
        assert_inactive(entry, items, details)


def test_delete_discards_unsent_changes_without_writing_them(database, monkeypatch):
    seed(database)
    manager, collection = make_manager(database, monkeypatch)
    writes = []
    original = collection.update_one

    def write(*args, **kwargs):
        writes.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(collection, "update_one", write)
    with manager:
        entry = manager.get(Entry, 1)
        entry.value = 8
        assert manager.delete(entry)
        assert not writes and collection.find_one({"_id": 1}) is None
        assert not manager.errors


def test_delete_cancels_an_unpersisted_document(database, monkeypatch):
    manager, collection = make_manager(database, monkeypatch)
    with manager:
        entry = manager.get(Entry, 1)
        entry.value = 8
        assert not manager.delete(entry)
        assert manager.stats["cached"] == 0
        assert collection.find_one({"_id": 1}) is None


def test_external_deletion_can_be_acknowledged_and_manager_can_close(database, monkeypatch):
    seed(database)
    seed(database, 2)
    manager, collection = make_manager(database, monkeypatch)
    with manager:
        entry = manager.get(Entry, 1)
        survivor = manager.get(Entry, 2)
        collection.delete_one({"_id": 1})
        entry.value = 8
        with pytest.raises(PersistenceError) as error:
            manager.wait_idle(5)
        assert isinstance(error.value.__cause__, ConcurrentModificationError)
        assert not manager.delete(entry)
        survivor.value = 9
        manager.wait_idle(5)
        assert not manager.errors and manager.stats["cached"] == 1
        assert collection.find_one({"_id": 1}) is None
        assert collection.find_one({"_id": 2})["value"] == 9


def test_delete_conflict_retains_cache_and_does_not_force_deletion(database, monkeypatch):
    seed(database)
    manager, collection = make_manager(database, monkeypatch)
    with manager:
        entry = manager.get(Entry, 1)
        entry.value = 8
        collection.update_one({"_id": 1}, {"$set": {"_orm_version": 2, "value": 500}})
        with pytest.raises(ConcurrentModificationError):
            manager.delete(entry)
        assert manager.get(Entry, 1) is entry and entry.value == 8 and entry.is_dirty
        assert collection.find_one({"_id": 1})["value"] == 500
        assert manager.release(entry, discard=True)
        assert collection.find_one({"_id": 1})["value"] == 500


@pytest.mark.parametrize("discard", [False, True])
def test_release_flushes_by_default_and_invalidates_old_references(database, monkeypatch, discard):
    seed(database)
    manager, collection = make_manager(database, monkeypatch)
    with manager:
        entry = manager.get(Entry, 1)
        items, details = entry.items, entry.details
        entry.value = 8
        assert manager.release(entry, discard=discard)
        assert manager.stats["cached"] == 0 and not manager.release(entry)
        assert_inactive(entry, items, details)
        with pytest.raises(DocumentInactiveError):
            entry.delete()
        assert collection.find_one({"_id": 1})["value"] == (0 if discard else 8)
        replacement = manager.get(Entry, 1)
        assert replacement is not entry and replacement.value == (0 if discard else 8)


def test_failed_release_preserves_pending_state_until_explicit_discard(database, monkeypatch):
    seed(database)
    manager, collection = make_manager(database, monkeypatch)

    def fail(*args, **kwargs):
        raise DuplicateKeyError("rejected write")

    with manager:
        entry = manager.get(Entry, 1)
        entry.value = 8
        with monkeypatch.context() as patch:
            patch.setattr(collection, "update_one", fail)
            with pytest.raises(PersistenceError):
                manager.release(entry)
        assert manager.get(Entry, 1) is entry and entry.is_dirty
        assert manager.errors
        assert manager.release(entry, discard=True)
        assert not manager.errors and collection.find_one({"_id": 1})["value"] == 0


def test_collection_model_registration_survives_release(database, monkeypatch):
    class OtherEntry(Document[int]):
        __collection__ = "entries"

    seed(database)
    manager, _ = make_manager(database, monkeypatch)
    with manager:
        assert manager.release(manager.get(Entry, 1))
        with pytest.raises(TypeError, match="One collection"):
            manager.get(OtherEntry, 1)
        assert isinstance(manager.get(Entry, 1), Entry)


def test_retirement_inside_synchronized_method_is_rejected(database, monkeypatch):
    seed(database)
    manager, _ = make_manager(database, monkeypatch)
    with manager:
        entry = manager.get(Entry, 1)
        with pytest.raises(RuntimeError, match="lock"):
            entry.delete_inside_method()
        assert manager.get(Entry, 1) is entry


@pytest.mark.parametrize("operation", ["release", "delete"])
def test_inflight_retirement_blocks_get_until_one_replacement_can_load(
    database, monkeypatch, operation
):
    seed(database)
    manager, collection = make_manager(database, monkeypatch, batch_size=1)
    entered, unblock = threading.Event(), threading.Event()
    extra_read = threading.Event()
    readers_ready = threading.Barrier(5)
    original_write, original_read = collection.update_one, collection.find_one
    writes, reads = [], []

    def write(query, update, **kwargs):
        writes.append(update)
        if len(writes) == 1:
            entered.set()
            assert unblock.wait(5)
        return original_write(query, update, **kwargs)

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        reads.append(result)
        if len(reads) > 1:
            extra_read.set()
        return result

    def get_after_barrier():
        readers_ready.wait(5)
        return manager.get(Entry, 1)

    monkeypatch.setattr(collection, "update_one", write)
    monkeypatch.setattr(collection, "find_one", read)
    with manager, ThreadPoolExecutor(max_workers=8) as pool:
        entry = manager.get(Entry, 1)
        items, details = entry.items, entry.details
        try:
            entry.value = 1
            assert entered.wait(5)
            entry.value = 2
            retirement = pool.submit(getattr(manager, operation), entry)
            wait_retiring(manager)
            readers = [pool.submit(get_after_barrier) for _ in range(4)]
            readers_ready.wait(5)
            assert not extra_read.wait(0.05)
            assert not retirement.done() and not any(job.done() for job in readers)
            with pytest.raises(DocumentInactiveError):
                entry.value = 3
            with pytest.raises(DocumentInactiveError, match="lock"):
                entry.load_inside_method(manager)
        finally:
            unblock.set()
        assert retirement.result(5)
        replacements = [job.result(5) for job in readers]
        replacement = replacements[0]
        assert all(item is replacement for item in replacements)
        assert replacement is not entry and manager.stats["cached"] == 1
        assert len(reads) == 2
        if operation == "release":
            assert reads[-1]["value"] == replacement.value == 2
            assert len(writes) == 2
        else:
            assert reads[-1] is None and replacement.value == 0
        assert_inactive(entry, items, details)


def test_retirement_timeout_restores_active_cache_identity(database, monkeypatch):
    seed(database)
    manager, collection = make_manager(database, monkeypatch, batch_size=1)
    entered, unblock = threading.Event(), threading.Event()
    original = collection.update_one

    def write(*args, **kwargs):
        entered.set()
        assert unblock.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(collection, "update_one", write)
    with manager:
        entry = manager.get(Entry, 1)
        try:
            entry.value = 1
            assert entered.wait(5)
            with pytest.raises(TimeoutError):
                manager.release(entry, timeout=0.02)
            assert manager.get(Entry, 1) is entry and manager.stats["retiring"] == 0
            entry.value = 2
        finally:
            unblock.set()
        manager.wait_idle(5)
        assert collection.find_one({"_id": 1})["value"] == 2


def test_lost_delete_acknowledgement_retains_cache_and_safe_retry_confirms_absence(
    database, monkeypatch
):
    seed(database)
    manager, collection = make_manager(database, monkeypatch)
    original = collection.delete_one

    def lose_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise AutoReconnect("deleted on server, acknowledgement lost")

    with manager:
        entry = manager.get(Entry, 1)
        entry.value = 8
        with monkeypatch.context() as patch:
            patch.setattr(collection, "delete_one", lose_ack)
            with pytest.raises(AutoReconnect):
                manager.delete(entry)
        assert manager.get(Entry, 1) is entry and entry.value == 8 and entry.is_dirty
        assert not manager.delete(entry)
        assert manager.stats["cached"] == 0


def test_delete_does_not_remove_recreated_document_at_the_same_version(database, monkeypatch):
    seed(database)
    manager, collection = make_manager(database, monkeypatch)
    with manager:
        entry = manager.get(Entry, 1)
        collection.delete_one({"_id": 1})
        seed(database)
        collection.update_one({"_id": 1}, {"$set": {"_orm_write_token": "replacement"}})
        with pytest.raises(ConcurrentModificationError):
            manager.delete(entry)
        assert collection.find_one({"_id": 1})["_orm_write_token"] == "replacement"
        assert manager.release(entry, discard=True)


@pytest.mark.parametrize(
    ("operation", "discard"), [("release", False), ("release", True), ("delete", True)]
)
def test_retirement_confirms_uncertain_push_before_evicting(
    database, monkeypatch, operation, discard
):
    seed(database)
    manager, collection = make_manager(database, monkeypatch, batch_size=1)
    entered, unblock = threading.Event(), threading.Event()
    original = collection.update_one
    calls = []

    def write(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            assert unblock.wait(5)
            raise AutoReconnect("array appended, acknowledgement lost")
        return result

    monkeypatch.setattr(collection, "update_one", write)
    with manager, ThreadPoolExecutor(max_workers=2) as pool:
        entry = manager.get(Entry, 1)
        try:
            entry.edit(1)
            assert entered.wait(5)
            entry.edit(2)
            options = {"discard": discard} if operation == "release" else {}
            retirement = pool.submit(getattr(manager, operation), entry, **options)
            wait_retiring(manager)
        finally:
            unblock.set()
        assert retirement.result(5)
        raw = collection.find_one({"_id": 1})
        if operation == "delete":
            assert raw is None
        elif discard:
            assert raw["value"] == 1 and raw["items"] == [1, -1]
            assert raw["_orm_version"] == 2
        else:
            assert raw["value"] == 2 and raw["items"] == [1, -1, 2, -2]
            assert raw["_orm_version"] == 3
        assert len(calls) >= 3 and not manager.errors


def test_close_waits_for_started_deletion(database, monkeypatch):
    seed(database)
    manager, collection = make_manager(database, monkeypatch)
    entered, unblock = threading.Event(), threading.Event()
    original = collection.delete_one

    def delete(*args, **kwargs):
        entered.set()
        assert unblock.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(collection, "delete_one", delete)
    entry = manager.get(Entry, 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            deletion = pool.submit(manager.delete, entry)
            assert entered.wait(5)
            closing = pool.submit(manager.close, 5)
            with manager._condition:
                assert manager._condition.wait_for(lambda: manager._closing, 5)
            assert not closing.done()
            with pytest.raises(ConnectionClosedError):
                manager.get(Entry, 1)
            with pytest.raises(ConnectionClosedError):
                entry.value = 9
        finally:
            unblock.set()
        assert deletion.result(5)
        closing.result(5)
        assert not manager._thread.is_alive()


def test_manager_worker_cannot_start_retirement(database, monkeypatch):
    seed(database)
    manager, _ = make_manager(database, monkeypatch)
    with manager:
        entry = manager.get(Entry, 1)

        def retire_in_worker():
            with pytest.raises(RuntimeError, match="worker"):
                manager.release(entry)

        manager._orm.submit(retire_in_worker).result(5)
        assert manager.get(Entry, 1) is entry


def test_startup_manager_supports_release_and_document_delete(database):
    init(
        client=database.client,
        database=database.name,
        flush_interval=60,
        check_connection=False,
    )
    try:
        entry = Entry.load(1)
        entry.value = 3
        assert get_manager().release(entry)
        replacement = Entry.load(1)
        assert replacement is not entry and replacement.value == 3
        assert replacement.delete()
        assert database.entries.find_one({"_id": 1}) is None
    finally:
        shutdown()
