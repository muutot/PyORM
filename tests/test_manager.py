import threading
import time
from concurrent.futures import ThreadPoolExecutor

import mongomock
import pytest
from pymongo.errors import AutoReconnect, DuplicateKeyError

from pyorm import (
    ConcurrentModificationError,
    ConnectionClosedError,
    Document,
    EmbeddedDocument,
    EmbeddedField,
    Field,
    Manager,
    PersistenceError,
)


class Stats(EmbeddedDocument):
    level = Field(int, default=1)
    exp = Field(int, default=0)


class Player(Document):
    __collection__ = "players"
    _autosave = ("name", "coins", "items", "tags")
    name = ""
    coins = 0
    items = []
    tags = set()
    stats = EmbeddedField(Stats, default_factory=Stats)

    def reward(self, coins):
        self.coins += coins
        self.stats.exp += 10


@pytest.fixture
def client():
    return mongomock.MongoClient()


def seed(client, id=1):
    client.tests.players.insert_one(
        {
            "_id": id,
            "_orm_version": 1,
            "name": "A",
            "coins": 0,
            "items": [],
            "tags": [],
            "stats": {"level": 1, "exp": 0},
            "unknown": "preserved",
        }
    )


def test_business_only_assigns_attributes_and_close_drains(client):
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        player = manager.get(Player, 1)
        player.name = "Alice"
        player.reward(25)
        assert player.coins == 25 and player.stats.exp == 10
    raw = client.tests.players.find_one({"_id": 1})
    assert raw["coins"] == 25 and raw["stats"] == {"level": 1, "exp": 10}


def test_identity_map_reads_only_once_under_concurrent_get(client, monkeypatch):
    seed(client)
    collection = client.tests.players
    reads = []
    original = collection.find_one

    def read(*args, **kwargs):
        reads.append(1)
        time.sleep(0.01)
        return original(*args, **kwargs)

    monkeypatch.setattr(collection, "find_one", read)
    with Manager(client=client, database="tests") as manager:
        with ThreadPoolExecutor(max_workers=16) as pool:
            objects = list(pool.map(lambda _: manager.get(Player, 1), range(200)))
        assert all(obj is objects[0] for obj in objects) and len(reads) == 1
        objects[0].coins = 8
        manager.wait_idle(5)
        assert manager.get(Player, 1).coins == 8 and len(reads) == 1


def test_timer_submits_automatically(client, monkeypatch):
    written = threading.Event()
    original = client.tests.players.update_one

    def write(*args, **kwargs):
        result = original(*args, **kwargs)
        written.set()
        return result

    monkeypatch.setattr(client.tests.players, "update_one", write)
    with Manager(client=client, database="tests", flush_interval=0.02, batch_size=100) as manager:
        player = manager.get(Player, 1)
        player.name = "Timer"
        assert written.wait(5)
        assert client.tests.players.find_one({"_id": 1})["name"] == "Timer"


def test_dirty_queue_threshold_submits_without_waiting_for_timer(client, monkeypatch):
    written = threading.Event()
    original = client.tests.players.update_one

    def write(*args, **kwargs):
        result = original(*args, **kwargs)
        written.set()
        return result

    monkeypatch.setattr(client.tests.players, "update_one", write)
    with Manager(client=client, database="tests", flush_interval=60, batch_size=2) as manager:
        manager.get(Player, 1).name = "One"
        assert not written.wait(0.02)
        manager.get(Player, 2).name = "Two"
        assert written.wait(5)


def test_writes_are_incremental_and_repeated_assignments_are_coalesced(client, monkeypatch):
    seed(client)
    writes = []
    original = client.tests.players.update_one

    def write(query, update, **kwargs):
        writes.append(update)
        return original(query, update, **kwargs)

    monkeypatch.setattr(client.tests.players, "update_one", write)
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        player = manager.get(Player, 1)
        for i in range(1000):
            player.coins = i
        player.stats.level = 5
        manager.wait_idle(5)
        assert len(writes) == 1
        assert writes[0]["$set"]["coins"] == 999
        assert writes[0]["$set"]["stats.level"] == 5
        assert "stats" not in writes[0]["$set"] and "name" not in writes[0]["$set"]
        assert "unknown" not in writes[0]["$set"]
        assert client.tests.players.find_one({"_id": 1})["unknown"] == "preserved"


def test_unchanged_fields_are_never_serialized(client, monkeypatch):
    seed(client)
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        player = manager.get(Player, 1)

        def unexpected(value):
            pytest.fail("An unchanged field was serialized")

        monkeypatch.setattr(Player._fields["stats"], "to_mongo", unexpected)
        player.coins = 10
        manager.wait_idle(5)
        assert client.tests.players.find_one({"_id": 1})["coins"] == 10


def test_new_assignments_during_io_stay_ahead_of_database(client, monkeypatch):
    seed(client)
    first, second = threading.Event(), threading.Event()
    release_first, release_second = threading.Event(), threading.Event()
    original = client.tests.players.update_one
    calls = []

    def write(*args, **kwargs):
        calls.append(1)
        event, release = (first, release_first) if len(calls) == 1 else (second, release_second)
        event.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(client.tests.players, "update_one", write)
    with Manager(client=client, database="tests", batch_size=1) as manager:
        player = manager.get(Player, 1)
        try:
            player.coins = 1
            assert first.wait(5)
            player.coins = 2  # This must not wait for the database operation's lock.
            assert player.coins == 2
            release_first.set()
            assert second.wait(5)
            assert client.tests.players.find_one({"_id": 1})["coins"] == 1
            assert manager.get(Player, 1) is player and player.coins == 2 and player.is_dirty
        finally:
            release_first.set()
            release_second.set()
        manager.wait_idle(5)
        assert player.coins == 2 and not player.is_dirty
        assert client.tests.players.find_one({"_id": 1})["coins"] == 2


@pytest.mark.parametrize("ack_lost_after_write", [False, True])
@pytest.mark.parametrize("new", [False, True])
def test_network_retry_is_idempotent_without_reloading(
    client, monkeypatch, ack_lost_after_write, new
):
    if not new:
        seed(client)
    original = client.tests.players.update_one
    updates = []
    errors = []

    def uncertain(query, update, **kwargs):
        updates.append(update)
        if len(updates) == 1:
            if ack_lost_after_write:
                original(query, update, **kwargs)
            raise AutoReconnect("simulated uncertain acknowledgement")
        return original(query, update, **kwargs)

    monkeypatch.setattr(client.tests.players, "update_one", uncertain)
    with Manager(
        client=client,
        database="tests",
        retry_interval=0.01,
        on_error=lambda doc, error: errors.append(error),
    ) as manager:
        player = manager.get(Player, 1)
        player.coins = 9
        manager.wait_idle(5)
        assert len(updates) == 2 and updates[0] == updates[1]
        raw = client.tests.players.find_one({"_id": 1})
        assert raw["coins"] == 9 and raw["_orm_version"] == (1 if new else 2)
        assert player.coins == 9 and not player.is_dirty and len(errors) == 1


def test_embedded_and_native_container_mutations_are_automatically_tracked(client):
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        player = manager.get(Player, 1)
        player.items.append({"count": 1})
        player.tags.add("vip")
        manager.wait_idle(5)
        player.items[0]["count"] = 3
        player.tags |= {"admin"}
        player.stats.exp = 7
        manager.wait_idle(5)
        raw = client.tests.players.find_one({"_id": 1})
        assert raw["items"] == [{"count": 3}] and set(raw["tags"]) == {"vip", "admin"}
        assert raw["stats"]["exp"] == 7


def test_unset_and_null_are_different(client):
    with Manager(client=client, database="tests") as manager:
        player = manager.get(Player, 1)
        manager.wait_idle(5)
        player.name = None
        del player.coins
        manager.wait_idle(5)
        raw = client.tests.players.find_one({"_id": 1})
        assert raw["name"] is None and "coins" not in raw


def test_partial_container_mutation_is_tracked_even_when_operation_raises(client):
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        player = manager.get(Player, 1)
        manager.wait_idle(5)

        def failing_iterator():
            yield {"count": 1}
            raise ValueError("iterator failed after first item")

        with pytest.raises(ValueError):
            player.items.extend(failing_iterator())
        assert player.is_dirty and player.items == [{"count": 1}]
        manager.wait_idle(5)
        assert client.tests.players.find_one({"_id": 1})["items"] == [{"count": 1}]


def test_direct_persistence_and_reload_are_rejected_for_managed_cache(client):
    with Manager(client=client, database="tests") as manager:
        player = manager.get(Player, 1)
        for operation in (player.save, player.save_async, player.reload, player.delete):
            with pytest.raises(RuntimeError):
                operation()


def test_permanent_failure_retains_cache_and_manager_remains_recoverable(client, monkeypatch):
    seed(client)
    errors = []
    manager = Manager(client=client, database="tests", on_error=lambda doc, e: errors.append(e))
    player = manager.get(Player, 1)
    original = client.tests.players.update_one

    def fail(*args, **kwargs):
        raise DuplicateKeyError("duplicate field")

    try:
        monkeypatch.setattr(client.tests.players, "update_one", fail)
        player.coins = 99
        with pytest.raises(PersistenceError):
            manager.close(5)
        assert manager.get(Player, 1) is player and player.coins == 99 and player.is_dirty
        assert manager.errors and errors
        monkeypatch.setattr(client.tests.players, "update_one", original)
        player.coins = 100
        manager.wait_idle(5)
    finally:
        monkeypatch.setattr(client.tests.players, "update_one", original)
        manager.close()


def test_close_blocks_new_assignments_and_closes_thread(client):
    manager = Manager(client=client, database="tests")
    player = manager.get(Player, 1)
    player.coins = 8
    manager.close()
    with pytest.raises(ConnectionClosedError):
        player.coins = 9
    with pytest.raises(ConnectionClosedError):
        player.items.append(1)
    with pytest.raises(ConnectionClosedError):
        manager.get(Player, 1)
    assert player.coins == 8 and not manager._thread.is_alive()
    manager.close()


def test_external_writer_cannot_replace_or_silently_overwrite_cache(client):
    seed(client)
    manager = Manager(client=client, database="tests", on_error=lambda *_: None)
    player = manager.get(Player, 1)
    try:
        client.tests.players.update_one({"_id": 1}, {"$set": {"coins": 500, "_orm_version": 2}})
        player.coins = 10
        with pytest.raises(PersistenceError) as error:
            manager.wait_idle(5)
        assert isinstance(error.value.__cause__, ConcurrentModificationError)
        assert player.coins == 10 and manager.get(Player, 1) is player and player.is_dirty
        assert client.tests.players.find_one({"_id": 1})["coins"] == 500
        # Resolve the external writer in this test; Manager itself never reloads.
        client.tests.players.update_one({"_id": 1}, {"$set": {"_orm_version": 1}})
        player.coins = 11
        manager.wait_idle(5)
    finally:
        manager.close()


def test_changes_while_a_failed_write_is_inflight_trigger_a_new_attempt(client, monkeypatch):
    seed(client)
    entered, release, succeeded = threading.Event(), threading.Event(), threading.Event()
    original = client.tests.players.update_one
    calls = []

    def write(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            raise DuplicateKeyError("old value rejected")
        result = original(*args, **kwargs)
        succeeded.set()
        return result

    monkeypatch.setattr(client.tests.players, "update_one", write)
    with Manager(
        client=client, database="tests", batch_size=1, on_error=lambda *_: None
    ) as manager:
        player = manager.get(Player, 1)
        try:
            player.coins = 8
            assert entered.wait(5)
            player.coins = 9
        finally:
            release.set()
        assert succeeded.wait(5)
        manager.wait_idle(5)
        assert player.coins == 9 and client.tests.players.find_one({"_id": 1})["coins"] == 9


def test_time_based_retry_does_not_require_lifecycle_barrier(client, monkeypatch):
    seed(client)
    original = client.tests.players.update_one
    succeeded = threading.Event()
    calls = []

    def write(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise AutoReconnect("retry on timer")
        result = original(*args, **kwargs)
        succeeded.set()
        return result

    monkeypatch.setattr(client.tests.players, "update_one", write)
    with Manager(
        client=client,
        database="tests",
        batch_size=1,
        flush_interval=60,
        retry_interval=0.01,
        on_error=lambda *_: None,
    ) as manager:
        manager.get(Player, 1).coins = 10
        assert succeeded.wait(5)


def test_same_field_reverted_while_inflight_does_not_lose_the_revert(client, monkeypatch):
    seed(client)
    entered, release = threading.Event(), threading.Event()
    original = client.tests.players.update_one
    calls = []

    def write(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(client.tests.players, "update_one", write)
    with Manager(client=client, database="tests", batch_size=1) as manager:
        player = manager.get(Player, 1)
        try:
            player.coins = 5
            assert entered.wait(5)
            player.coins = 0
        finally:
            release.set()
        manager.wait_idle(5)
        assert player.coins == 0 and client.tests.players.find_one({"_id": 1})["coins"] == 0
        assert len(calls) == 2


def test_workers_persist_different_documents_in_parallel(client, monkeypatch):
    barrier = threading.Barrier(2)
    original = client.tests.players.update_one
    threads = []

    def write(*args, **kwargs):
        threads.append(threading.get_ident())
        barrier.wait(5)
        return original(*args, **kwargs)

    with Manager(client=client, database="tests", workers=2, flush_interval=60) as manager:
        # Choose ids mapped to different worker lanes using the executor's stable hash.
        import hashlib

        from bson import BSON

        def lane(id):
            digest = hashlib.sha256(BSON.encode({"key": ("players", id)})).digest()
            return int.from_bytes(digest[:8], "big") % 2

        second = next(i for i in range(2, 100) if lane(i) != lane(1))
        monkeypatch.setattr(client.tests.players, "update_one", write)
        manager.get(Player, 1).coins = 1
        manager.get(Player, second).coins = 2
        manager.wait_idle(5)
        assert len(set(threads)) == 2


def test_repeated_mutations_keep_first_flush_deadline(client, monkeypatch):
    seed(client)
    written = threading.Event()
    original = client.tests.players.update_one

    def write(*args, **kwargs):
        result = original(*args, **kwargs)
        written.set()
        return result

    with Manager(client=client, database="tests", flush_interval=0.05) as manager:
        player = manager.get(Player, 1)
        monkeypatch.setattr(client.tests.players, "update_one", write)
        stop = threading.Event()

        def mutate():
            value = 0
            while not stop.wait(0.005):
                value += 1
                player.coins = value

        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(mutate)
            try:
                assert written.wait(5)  # Writes while mutations continue, before shutdown.
            finally:
                stop.set()
            job.result(5)
        manager.wait_idle(5)
        assert client.tests.players.find_one({"_id": 1})["coins"] == player.coins


@pytest.mark.parametrize("setting", ["batch_size", "flush_interval"])
def test_pending_mutation_wakes_dispatcher_after_schedule_setting_changes(
    client, monkeypatch, setting
):
    seed(client)
    waiting, written = threading.Event(), threading.Event()
    original = client.tests.players.update_one
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        player = manager.get(Player, 1)
        original_wait = manager._condition.wait

        def observe_wait(timeout=None):
            waiting.set()
            return original_wait(timeout)

        def write(*args, **kwargs):
            result = original(*args, **kwargs)
            written.set()
            return result

        monkeypatch.setattr(manager._condition, "wait", observe_wait)
        monkeypatch.setattr(client.tests.players, "update_one", write)
        player.coins = 1
        assert waiting.wait(5)
        setattr(manager, setting, 1 if setting == "batch_size" else 0.01)
        player.coins = 2
        assert written.wait(5)
        manager.wait_idle(5)
        assert client.tests.players.find_one({"_id": 1})["coins"] == 2


def test_queue_capacity_one_drains_all_documents_and_inflight_edits(client, monkeypatch):
    for id in range(20):
        seed(client, id)
    entered, release = threading.Event(), threading.Event()
    original = client.tests.players.update_one
    writes = []

    def write(query, update, **kwargs):
        writes.append(query["_id"])
        if len(writes) == 1:
            entered.set()
            assert release.wait(5)
        return original(query, update, **kwargs)

    with Manager(
        client=client, database="tests", workers=4, max_pending=1, batch_size=1
    ) as manager:
        players = [manager.get(Player, id) for id in range(20)]
        monkeypatch.setattr(client.tests.players, "update_one", write)
        players[0].coins = 1
        try:
            assert entered.wait(5)
            for player in players:
                player.coins = 2
            assert manager.stats["inflight"] == 1
        finally:
            release.set()
        manager.wait_idle(5)
        assert len(writes) == 21 and client.tests.players.count_documents({"coins": 2}) == 20
        assert all(not player.is_dirty for player in players)


def test_mutable_bson_leaf_is_detached_before_worker_and_acknowledgement(client, monkeypatch):
    from bson import Code

    class Script(Document):
        script = Field(Code)

    collection = client.tests.script
    collection.insert_one({"_id": 1, "_orm_version": 1, "script": Code("return n", {"n": 0})})
    entered, release = threading.Event(), threading.Event()
    with Manager(client=client, database="tests", batch_size=1) as manager:
        script = manager.get(Script, 1)
        original = manager._persist
        calls = []

        def persist(document, packet):
            calls.append(packet)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            return original(document, packet)

        monkeypatch.setattr(manager, "_persist", persist)
        value = Code("return n", {"n": 1})
        script.script = value
        try:
            assert entered.wait(5)
            value.scope["n"] = 2
            script.script = value
        finally:
            release.set()
        manager.wait_idle(5)
        assert calls[0].update["$set"]["script"].scope == {"n": 1}
        assert script.script.scope == collection.find_one({"_id": 1})["script"].scope == {"n": 2}
        value.scope["n"] = 3
        script.script = value
        manager.wait_idle(5)
        assert script.version == 4 and collection.find_one({"_id": 1})["script"].scope == {"n": 3}


@pytest.mark.parametrize("batch_size", [1, 3, 10**30])
def test_automatic_scheduling_handles_thresholds_above_and_below_capacity(
    client, monkeypatch, batch_size
):
    for id in range(5):
        seed(client, id)
    written = threading.Event()
    original = client.tests.players.update_one
    completed = []

    def write(query, update, **kwargs):
        result = original(query, update, **kwargs)
        completed.append(query["_id"])
        if len(completed) == 5:
            written.set()
        return result

    monkeypatch.setattr(client.tests.players, "update_one", write)
    with Manager(
        client=client,
        database="tests",
        workers=1,
        max_pending=4,
        flush_interval=0.02,
        batch_size=batch_size,
    ) as manager:
        players = [manager.get(Player, id) for id in range(5)]
        for player in players:
            player.coins = 1
        assert written.wait(5)
        assert client.tests.players.count_documents({"coins": 1}) == 5


def test_blocked_and_delayed_documents_do_not_starve_healthy_work(client, monkeypatch):
    for id in range(3):
        seed(client, id)
    healthy_written = threading.Event()
    original = client.tests.players.update_one
    failed = set()
    retry_times = []

    def write(query, update, **kwargs):
        id = query["_id"]
        if id in (0, 1) and id not in failed:
            failed.add(id)
            if id == 0:
                raise ValueError("permanent failure")
            retry_times.append(time.monotonic())
            raise AutoReconnect("delayed retry")
        if id == 1:
            retry_times.append(time.monotonic())
        result = original(query, update, **kwargs)
        if id == 2:
            healthy_written.set()
        return result

    monkeypatch.setattr(client.tests.players, "update_one", write)
    with Manager(
        client=client,
        database="tests",
        workers=1,
        max_pending=1,
        batch_size=1,
        retry_interval=0.05,
        on_error=lambda *_: None,
    ) as manager:
        players = [manager.get(Player, id) for id in range(3)]
        try:
            for player in players:
                player.coins = 1
            assert healthy_written.wait(5)
            with pytest.raises(PersistenceError):
                manager.wait_idle(5)
        finally:
            players[0].coins = 2
        manager.wait_idle(5)
        assert retry_times[1] - retry_times[0] >= 0.045
        assert [client.tests.players.find_one({"_id": id})["coins"] for id in range(3)] == [2, 1, 1]
