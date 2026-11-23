import threading
from concurrent.futures import ThreadPoolExecutor

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

import pyorm.runtime as runtime
from pyorm import (
    ConnectionClosedError,
    Document,
    EmbeddedDocument,
    EmbeddedField,
    ManagerNotInitializedError,
    PersistenceError,
    get_manager,
    init,
    shutdown,
)


class Player(Document):
    _autosave = ("name", "coins")
    name = ""
    coins = 0

    def reward(self, amount):
        self.coins += amount


@pytest.fixture(autouse=True)
def isolated_runtime():
    shutdown()
    yield
    shutdown()


def test_business_model_requires_startup_initialization():
    with pytest.raises(ManagerNotInitializedError, match="startup"):
        Player.load(1)
    with pytest.raises(ManagerNotInitializedError):
        get_manager()
    assert runtime._manager is None


def test_startup_init_then_business_only_loads_and_assigns():
    client = mongomock.MongoClient()
    init(client=client, database="game")
    player = Player.load(1)
    player.name = "Alice"
    player.reward(10)
    assert Player.load(1) is player and player._manager is get_manager()
    shutdown()
    assert client.game.player.find_one({"_id": 1})["coins"] == 10
    with pytest.raises(ConnectionClosedError):
        player.coins = 20


def test_embedded_field_autosaves_without_autosave_entry_and_restores(monkeypatch):
    class Stats(EmbeddedDocument):
        _autosave = ("exp",)
        exp = 0

    class Pet(Document):
        __collection__ = "pet"
        _autosave = ("name",)
        name = ""
        stats = EmbeddedField(Stats)

    client = mongomock.MongoClient()
    manager = init(client=client, database="game", flush_interval=60)
    pet = Pet.load(1)
    pet.name = "Mimi"
    pet.stats.exp = 10
    shutdown()
    assert client.game.pet.find_one({"_id": 1})["stats"] == {"exp": 10}

    manager = init(client=client, database="game", flush_interval=60)
    pet = Pet.load(1)
    assert isinstance(pet.stats, Stats) and pet.stats.exp == 10
    writes = []
    collection = manager._orm._collection(Pet)
    original = collection.update_one

    def write(query, update, **kwargs):
        writes.append(update)
        return original(query, update, **kwargs)

    def unexpected_read(*args, **kwargs):
        pytest.fail("Cached embedded edits must not reload business data")

    with monkeypatch.context() as patch:
        patch.setattr(collection, "update_one", write)
        patch.setattr(collection, "find_one", unexpected_read)
        pet.stats.exp += 5
        assert Pet.load(1) is pet and pet.stats.exp == 15
        manager.wait_idle(5)
    assert len(writes) == 1
    business_set = {k: v for k, v in writes[0]["$set"].items() if not k.startswith("_orm_")}
    assert business_set == {"stats.exp": 15}
    assert client.game.pet.find_one({"_id": 1})["stats"] == {"exp": 15}


def test_concurrent_initialization_creates_one_manager(monkeypatch):
    client = mongomock.MongoClient()
    original = runtime.Manager
    created = []

    def create(*args, **kwargs):
        manager = original(*args, **kwargs)
        created.append(manager)
        return manager

    monkeypatch.setattr(runtime, "Manager", create)
    with ThreadPoolExecutor(max_workers=16) as pool:
        managers = list(pool.map(lambda _: init(client=client, database="game"), range(100)))
        players = list(pool.map(lambda _: Player.load(1), range(100)))
    assert len(created) == 1 and all(manager is created[0] for manager in managers)
    assert all(player is players[0] for player in players)


def test_duplicate_init_does_not_replace_cache_or_change_database():
    client = mongomock.MongoClient()
    manager = init(client=client, database="one")
    player = Player.load(1)
    player.coins = 25
    assert init(client=client, database="one") is manager
    with pytest.raises(RuntimeError, match="different settings"):
        init(client=client, database="two")
    assert Player.load(1) is player and player.coins == 25


def test_startup_checks_connection_once(monkeypatch):
    client = mongomock.MongoClient()
    calls = []
    original = client.admin.command

    def command(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(client.admin, "command", command)
    init(client=client)
    init(client=client)
    assert calls == [("ping",)]


def test_failed_startup_releases_manager_and_allows_retry(monkeypatch):
    client = mongomock.MongoClient()
    original = runtime.Manager
    created = []

    def create(*args, **kwargs):
        manager = original(*args, **kwargs)
        created.append(manager)
        return manager

    def offline(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(runtime, "Manager", create)
    with monkeypatch.context() as patch:
        patch.setattr(client.admin, "command", offline)
        with pytest.raises(OSError, match="offline"):
            init(client=client)
    assert runtime._manager is None and not created[0]._thread.is_alive()
    assert init(client=client) is created[1]


def test_failed_shutdown_retains_default_manager_and_cache(monkeypatch):
    client = mongomock.MongoClient()
    manager = init(client=client, database="game", on_error=lambda *_: None)
    player = Player.load(1)
    player.coins = 5

    def fail(*args, **kwargs):
        raise DuplicateKeyError("rejected")

    with monkeypatch.context() as patch:
        patch.setattr(client.game.player, "update_one", fail)
        with pytest.raises(PersistenceError):
            shutdown()
        assert get_manager() is manager and Player.load(1) is player and player.coins == 5
    player.coins = 6
    shutdown()
    assert runtime._manager is None and client.game.player.find_one({"_id": 1})["coins"] == 6


def test_shutdown_does_not_deadlock_runtime_calls_from_worker_callbacks(monkeypatch):
    client = mongomock.MongoClient()
    manager = init(client=client, database="game", batch_size=1)
    entered, release, callback = threading.Event(), threading.Event(), threading.Event()
    original = client.game.player.update_one

    def write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        with pytest.raises(ConnectionClosedError):
            get_manager()
        callback.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(client.game.player, "update_one", write)
    Player.load(1).coins = 7
    assert entered.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopped = pool.submit(shutdown)
        with manager._condition:
            assert manager._condition.wait_for(lambda: manager._closing, timeout=5)
        with pytest.raises(ConnectionClosedError):
            init(client=client, database="game", batch_size=1)
        release.set()
        stopped.result(5)
    assert callback.is_set() and runtime._manager is None


def test_explicit_shutdown_then_restart_loads_a_new_cache():
    client = mongomock.MongoClient()
    init(client=client, database="game")
    player = Player.load(1)
    player.coins = 50
    shutdown()
    shutdown()
    init(client=client, database="game")
    loaded = Player.load(1)
    assert loaded is not player and loaded.coins == 50
