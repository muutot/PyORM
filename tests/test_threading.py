import threading
from concurrent.futures import ThreadPoolExecutor

import mongomock
import pytest

from pyorm import ConnectionClosedError, MongoORM, QueueFullError
from pyorm.executor import OrderedExecutor


def test_shared_client_and_parallel_crud(players):
    shared = players.orm.client

    def work(i):
        player = players.create(id=i, name=str(i))
        assert players.orm.client is shared
        player.coins = i
        player.save()
        return players.get(i).coins

    with ThreadPoolExecutor(max_workers=16) as executor:
        assert list(executor.map(work, range(200))) == list(range(200))
    assert players.filter().count() == 200


def test_shared_document_lock_protects_nested_mutation(players):
    player = players.create(name="A", inventory={"counter": 0})

    def increment(_):
        with player.locked():
            player.inventory["counter"] += 1
            player.save()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(increment, range(100)))
    assert players.get(player.id).inventory["counter"] == 100


def test_same_key_fifo_order_and_failure_does_not_block_following_jobs(orm):
    order = []

    def record(i):
        order.append(i)
        return i

    futures = [orm.submit(record, i, key=("players", "abc")) for i in range(100)]
    assert [future.result(5) for future in futures] == list(range(100))
    assert order == list(range(100))

    def fail():
        raise ValueError("visible error")

    failed = orm.submit(fail, key=1)
    following = orm.submit(lambda: 7, key=1)
    with pytest.raises(ValueError, match="visible error"):
        failed.result(5)
    assert following.result(5) == 7
    with pytest.raises(ValueError, match="visible error"):
        orm.flush(5)
    orm.flush(5)


def test_workers_actually_run_in_parallel(orm):
    barrier = threading.Barrier(2)

    def meet():
        barrier.wait(timeout=5)
        return threading.get_ident()

    first, second = orm.submit(meet), orm.submit(meet)
    assert first.result(5) != second.result(5)


def test_queue_bound_cancellation_and_flush_timeout():
    executor = OrderedExecutor(workers=1, max_pending=2)
    started, release = threading.Event(), threading.Event()

    def block():
        started.set()
        assert release.wait(5)

    try:
        running = executor.submit(block)
        assert started.wait(5)
        queued = executor.submit(lambda: 2)
        with pytest.raises(QueueFullError):
            executor.submit(lambda: 3)
        with pytest.raises(TimeoutError):
            executor.flush(timeout=0.01)
        assert queued.cancel()
        replacement = executor.submit(lambda: 4)
        release.set()
        running.result(5)
        assert replacement.result(5) == 4
        executor.flush(5)
    finally:
        release.set()
        executor.shutdown()


def test_close_drains_and_rejects_new_work():
    client = mongomock.MongoClient()
    orm = MongoORM(client=client, max_pending=16)
    started, release, closing = threading.Event(), threading.Event(), threading.Event()
    output = []

    def block():
        started.set()
        assert release.wait(5)
        output.append(1)

    first = orm.submit(block, key=1)
    second = orm.submit(lambda: output.append(2), key=1)
    assert started.wait(5)

    def close():
        closing.set()
        orm.close()

    thread = threading.Thread(target=close)
    thread.start()
    assert closing.wait(5)
    # Observe the closing transition while the queued worker is still blocked.
    with orm._condition:
        assert orm._condition.wait_for(lambda: orm._closing, timeout=5)
    with pytest.raises(ConnectionClosedError):
        orm.submit(lambda: None)
    with pytest.raises(ConnectionClosedError):
        orm._run(lambda: None)
    release.set()
    first.result(5)
    second.result(5)
    thread.join(5)
    assert not thread.is_alive() and output == [1, 2] and orm.is_closed
    orm.close()
    with pytest.raises(ConnectionClosedError):
        orm.ping()


def test_close_waits_for_synchronous_operation():
    orm = MongoORM(client=mongomock.MongoClient())
    entered, release = threading.Event(), threading.Event()

    def operation():
        entered.set()
        assert release.wait(5)

    worker = threading.Thread(target=lambda: orm._run(operation))
    worker.start()
    assert entered.wait(5)
    closer = threading.Thread(target=orm.close)
    closer.start()
    assert closer.is_alive()
    release.set()
    worker.join(5)
    closer.join(5)
    assert orm.is_closed and not closer.is_alive()


def test_shutdown_failure_still_closes_owned_client(monkeypatch):
    client = mongomock.MongoClient()
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    orm = MongoORM(client=client, owns_client=True)

    def fail():
        raise ValueError("failed job")

    future = orm.submit(fail)
    with pytest.raises(ValueError):
        future.result(5)
    with pytest.raises(ValueError, match="failed job"):
        orm.close()
    assert orm.is_closed and closed == [True]


def test_external_client_is_not_closed(monkeypatch):
    client = mongomock.MongoClient()
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    MongoORM(client=client).close()
    assert not closed


def test_worker_cannot_submit_flush_or_close_itself(orm):
    def bad_actions():
        for action in (lambda: orm.submit(lambda: None), orm.flush, orm.close):
            with pytest.raises(RuntimeError):
                action()
        return 1

    assert orm.submit(bad_actions).result(5) == 1
    assert not orm.is_closed


def test_flush_concurrent_with_cancel_pending_shutdown():
    executor = OrderedExecutor(workers=1)
    started, release, waiting = threading.Event(), threading.Event(), threading.Event()

    def block():
        started.set()
        assert release.wait(5)

    first = executor.submit(block)
    assert started.wait(5)
    pending = executor.submit(lambda: None)
    original_wait = executor._condition.wait

    def observed_wait(timeout=None):
        waiting.set()
        return original_wait(timeout)

    executor._condition.wait = observed_wait
    with ThreadPoolExecutor(max_workers=2) as pool:
        flushed = pool.submit(executor.flush, 5)
        assert waiting.wait(5)
        cancelled = threading.Event()
        pending.add_done_callback(lambda _: cancelled.set())
        closed = pool.submit(executor.shutdown, cancel_pending=True)
        assert cancelled.wait(5)
        release.set()
        first.result(5)
        closed.result(5)
        flushed.result(5)
    assert pending.cancelled()


def test_unacknowledged_writes_and_pool_settings_rejected():
    with pytest.raises(ValueError, match="acknowledged"):
        MongoORM("mongodb://localhost:27017/?w=0")
    with pytest.raises(ValueError):
        MongoORM(max_pool_size=0)
    with pytest.raises(ValueError):
        MongoORM(min_pool_size=101)


def test_context_preserves_body_exception_and_closes():
    orm = MongoORM(client=mongomock.MongoClient())

    def fail():
        raise ValueError("worker failed")

    with pytest.raises(KeyError, match="body"):
        with orm:
            future = orm.submit(fail)
            with pytest.raises(ValueError):
                future.result(5)
            raise KeyError("body")
    assert orm.is_closed


def test_save_async_and_close_cancels_pending(players):
    player = players.new(id=99, name="Async")
    assert player.save_async().result(5) is player
    assert players.get(99).name == "Async"

    executor = OrderedExecutor(workers=1)
    started, release = threading.Event(), threading.Event()

    def block():
        started.set()
        assert release.wait(5)

    first = executor.submit(block)
    assert started.wait(5)
    pending = executor.submit(lambda: None)
    cancelled = threading.Event()
    pending.add_done_callback(lambda _: cancelled.set())
    with ThreadPoolExecutor(max_workers=1) as closer:
        closed = closer.submit(executor.shutdown, cancel_pending=True)
        # cancel_futures is called before waiting for the running operation.
        assert cancelled.wait(5)
        release.set()
        closed.result(5)
    first.result(5)
    assert pending.cancelled()
