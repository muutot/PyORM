"""Real MongoDB checks: set PYORM_TEST_URI to a disposable/local MongoDB instance."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from pymongo import IndexModel
from pymongo.errors import DuplicateKeyError

from pyorm import ConcurrentModificationError, Document, Field, Manager, MongoORM

pytestmark = pytest.mark.integration


@pytest.fixture
def real_orm():
    uri = os.environ.get("PYORM_TEST_URI")
    if not uri:
        pytest.skip("PYORM_TEST_URI is not set")
    database = "pyorm_test_" + uuid.uuid4().hex
    orm = MongoORM(uri, database, workers=8, max_pool_size=32, max_pending=2048)
    try:
        orm.ping()
        yield orm
    finally:
        assert database.startswith("pyorm_test_")
        orm.client.drop_database(database)
        orm.close()


class Counter(Document):
    name = Field(str, required=True)
    count = Field(int, default=0)
    data = Field(dict, default_factory=dict)
    __indexes__ = (IndexModel("name", unique=True),)


def test_real_pool_atomic_increments_and_conflict(real_orm):
    repo = real_orm[Counter]
    repo.create(id=1, name="counter", data={"items": [{"x": 1}, {"x": 1}]})
    stale = repo.get(1)

    def increment(_):
        return repo.filter(id=1).update(inc__count=1).modified_count

    with ThreadPoolExecutor(max_workers=16) as executor:
        assert sum(executor.map(increment, range(500))) == 500
    loaded = repo.get(1)
    assert loaded.count == 500 and loaded.version == 501
    stale.count = -1
    with pytest.raises(ConcurrentModificationError):
        stale.save()
    loaded.data["items"].pop(0)
    loaded.data["items"][0]["x"] = 2
    loaded.save()
    assert repo.get(1).data == {"items": [{"x": 2}]}
    assert loaded.delete()


def test_real_threaded_inserts_and_ordered_updates(real_orm):
    repo = real_orm[Counter]
    repo.ensure_indexes()
    jobs = [real_orm.submit(repo.create, id=i, name=f"n{i}", key=i) for i in range(200)]
    assert len([job.result(15) for job in jobs]) == 200
    real_orm.flush(15)
    jobs = [
        real_orm.submit(repo.update_one, {"_id": 1}, count=i, key=("counter", 1))
        for i in range(100)
    ]
    assert [job.result(15).count for job in jobs] == list(range(100))
    real_orm.flush(15)
    assert repo.get(1).count == 99
    with pytest.raises(DuplicateKeyError):
        repo.create(name="n1")
    assert repo.aggregate([{"$count": "total"}]) == [{"total": 200}]


def test_real_manager_cache_is_ahead_during_incremental_writes(real_orm, monkeypatch):
    repo = real_orm[Counter]
    repo.create(id=1, name="managed")
    collection = real_orm._collection(Counter)
    original = collection.update_one
    entered, release = threading.Event(), threading.Event()
    updates = []

    def write(query, update, **kwargs):
        updates.append(update)
        if len(updates) == 1:
            entered.set()
            assert release.wait(5)
        return original(query, update, **kwargs)

    with Manager(client=real_orm.client, database=real_orm._database.name, batch_size=1) as manager:
        # Bind the cached Collection object used by this Manager before spying.
        monkeypatch.setattr(manager._orm, "_collection", lambda _: collection)
        monkeypatch.setattr(collection, "update_one", write)
        counter = manager.get(Counter, 1)
        try:
            counter.count = 1
            assert entered.wait(5)
            counter.count = 2
            assert manager.get(Counter, 1) is counter and counter.count == 2
        finally:
            release.set()
        manager.wait_idle(5)
        assert counter.count == 2 and not counter.is_dirty
        assert repo.get(1).count == 2
        assert len(updates) == 2 and "name" not in updates[-1]["$set"]


def test_real_manager_retries_lost_ack_without_reload(real_orm, monkeypatch):
    from pymongo.errors import AutoReconnect

    with Manager(
        client=real_orm.client,
        database=real_orm._database.name,
        retry_interval=0.01,
        on_error=lambda *_: None,
    ) as manager:
        collection = manager._orm._collection(Counter)
        monkeypatch.setattr(manager._orm, "_collection", lambda _: collection)
        original = collection.update_one
        calls = []

        def write(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                raise AutoReconnect("server wrote, acknowledgement lost")
            return result

        monkeypatch.setattr(collection, "update_one", write)
        counter = manager.get(Counter, 1)
        counter.name = "new"
        counter.count = 5
        manager.wait_idle(5)
        assert len(calls) == 2 and counter.version == 1 and counter.count == 5
        assert real_orm[Counter].get(1).version == 1


def test_real_pet_container_fields_restore_types_and_write_incrementally(real_orm, monkeypatch):
    from examples.quickstart import Pet, PetStats

    database = real_orm._database.name
    with Manager(client=real_orm.client, database=database, flush_interval=60) as manager:
        pet = manager.get(Pet, "pet-1")
        pet.name = "Mimi"
        pet.learn_skill("scratch")
        pet.set_attack(25)
        pet.move_to(10, 20)
        pet.mark_rare()

    collection = real_orm._collection(Pet)
    raw = collection.find_one({"_id": "pet-1"})
    assert raw["skills"] == ["scratch"] and raw["attributes"] == {"attack": 25}
    assert raw["position"] == [10, 20] and raw["tags"] == ["rare"]

    with Manager(client=real_orm.client, database=database, flush_interval=60) as manager:
        monkeypatch.setattr(manager._orm, "_collection", lambda _: collection)
        pet = manager.get(Pet, "pet-1")
        assert isinstance(pet.stats, PetStats)
        assert isinstance(pet.skills, list) and isinstance(pet.attributes, dict)
        assert isinstance(pet.position, tuple) and isinstance(pet.tags, set)
        assert manager.get(Pet, "pet-1") is pet

        writes = []
        original = collection.update_one

        def write(query, update, **kwargs):
            writes.append(update)
            return original(query, update, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(collection, "update_one", write)
            pet.learn_skill("jump")
            del pet.attributes["attack"]
            pet.move_to(30, 40)
            pet.tags.add("trained")
            manager.wait_idle(5)

        assert len(writes) == 1
        business_set = {k: v for k, v in writes[0]["$set"].items() if not k.startswith("_orm_")}
        assert business_set == {
            "skills.1": "jump",
            "position": [30, 40],
            "tags.1": "trained",
        }
        assert writes[0]["$unset"] == {"attributes.attack": ""}
        assert pet.position == (30, 40) and pet.tags == {"rare", "trained"}

    raw = collection.find_one({"_id": "pet-1"})
    assert raw["attributes"] == {} and raw["name"] == "Mimi"


def test_real_push_retry_confirms_receipt_without_duplicating_elements(real_orm, monkeypatch):
    from pymongo.errors import AutoReconnect

    from examples.quickstart import Pet

    database = real_orm._database.name
    with Manager(client=real_orm.client, database=database, flush_interval=60) as manager:
        pet = manager.get(Pet, "pet-1")
        pet.skills.append("old")
        pet.tags.add("rare")

    collection = real_orm._collection(Pet)
    with Manager(
        client=real_orm.client,
        database=database,
        flush_interval=60,
        retry_interval=0.01,
        on_error=lambda *_: None,
    ) as manager:
        monkeypatch.setattr(manager._orm, "_collection", lambda _: collection)
        pet = manager.get(Pet, "pet-1")
        original = collection.update_one
        updates = []

        def write(query, update, **kwargs):
            result = original(query, update, **kwargs)
            updates.append(update)
            if len(updates) == 1:
                raise AutoReconnect("server appended, acknowledgement lost")
            return result

        def unexpected_read(*args, **kwargs):
            pytest.fail("Array retry must not reload business data")

        with monkeypatch.context() as patch:
            patch.setattr(collection, "update_one", write)
            patch.setattr(collection, "find_one", unexpected_read)
            pet.skills.extend(["jump", "run"])
            pet.tags.update({"trained", "young"})
            manager.wait_idle(5)
        assert len(updates) == 3 and updates[0] == updates[1]
        assert updates[0]["$push"]["skills"] == {"$each": ["jump", "run"]}
        assert "$push" not in updates[2] and pet.version == 2

    raw = collection.find_one({"_id": "pet-1"})
    assert raw["skills"] == ["old", "jump", "run"] and raw["_orm_version"] == 2
    assert len(raw["tags"]) == 3 and set(raw["tags"]) == {"rare", "trained", "young"}


def test_real_sustained_concurrent_mutations_with_bounded_queue_and_lost_acks(
    real_orm, monkeypatch
):
    from pymongo.errors import AutoReconnect

    class Stress(Document[int]):
        count = Field(int, default=0)
        items = Field(list, default_factory=list)
        data = Field(dict, default_factory=lambda: {"count": 0})
        tags = Field(set, default_factory=set)

        def reward(self, value):
            self.count += 1
            self.items.extend([value, -value])
            self.data["count"] += 1
            self.tags.add(str(value % 10))

    collection = real_orm._collection(Stress)
    collection.insert_many(
        {
            "_id": i,
            "_orm_version": 1,
            "count": 0,
            "items": [],
            "data": {"count": 0},
            "tags": [],
            "unknown": "preserved",
        }
        for i in range(16)
    )
    seen = set()
    lost = []
    lock = threading.Lock()
    original = collection.update_one

    def write(query, update, **kwargs):
        result = original(query, update, **kwargs)
        token = update["$set"]["_orm_write_token"]
        with lock:
            first = token not in seen
            seen.add(token)
            lose = first and len(seen) % 4 == 0
            if lose:
                lost.append(token)
        if lose:
            raise AutoReconnect("acknowledgement lost after successful server write")
        return result

    with Manager(
        client=real_orm.client,
        database=real_orm._database.name,
        workers=8,
        max_pending=2,
        batch_size=3,
        flush_interval=0.01,
        retry_interval=0.01,
        on_error=lambda *_: None,
    ) as manager:
        monkeypatch.setattr(manager._orm, "_collection", lambda _: collection)
        documents = [manager.get(Stress, id) for id in range(16)]
        monkeypatch.setattr(collection, "update_one", write)
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(documents[value % 16].reward, value) for value in range(512)]
            for future in futures:
                future.result(10)
        manager.wait_idle(15)
        assert lost and not manager.errors
        for id, document in enumerate(documents):
            raw = collection.find_one({"_id": id})
            expected = list(range(id, 512, 16))
            assert raw["count"] == document.count == 32
            assert raw["data"] == document.data == {"count": 32}
            assert raw["items"] == document.items and len(raw["items"]) == 64
            assert sorted(raw["items"][::2]) == expected
            assert all(b == -a for a, b in zip(raw["items"][::2], raw["items"][1::2], strict=True))
            assert set(raw["tags"]) == document.tags == {str(value % 10) for value in expected}
            assert raw["unknown"] == "preserved" and raw["_orm_version"] == document.version
            assert manager.get(Stress, id) is document and not document.is_dirty
