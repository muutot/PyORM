import threading
import time
from concurrent.futures import ThreadPoolExecutor

import mongomock
import pytest

from pyorm import Document, EmbeddedDocument, EmbeddedField, Field, Manager


class Stats(EmbeddedDocument):
    exp = Field(int, default=0)

    def gain_exp(self):
        value = self.exp
        time.sleep(0.0001)
        self.exp = value + 1


class Player(Document):
    _autosave = ("coins", "level")
    coins = 0
    level = 0
    stats = EmbeddedField(Stats, default_factory=Stats)

    def reward(self):
        value = self.coins
        time.sleep(0.0001)  # Release the GIL between reading and assigning.
        self.coins = value + 1

    def reward_and_gain_exp(self):
        self.reward()
        self.stats.gain_exp()

    def fail_after_assigning(self):
        self.coins = 5
        raise ValueError("business error")

    def change_two_fields(self, entered, release):
        self.coins = 1
        entered.set()
        assert release.wait(5)
        self.level = 1


def test_business_methods_need_no_explicit_lock_under_concurrent_calls():
    client = mongomock.MongoClient()
    with Manager(client=client, database="tests") as manager:
        player = manager.get(Player, 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: player.reward_and_gain_exp(), range(200)))
        assert player.coins == 200 and player.stats.exp == 200
    raw = client.tests.player.find_one({"_id": 1})
    assert raw["coins"] == 200 and raw["stats"]["exp"] == 200


def test_method_exception_releases_lock_and_preserves_changes():
    with Manager(client=mongomock.MongoClient(), database="tests") as manager:
        player = manager.get(Player, 1)
        with pytest.raises(ValueError):
            player.fail_after_assigning()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(player.reward).result(5)
        assert player.coins == 6
        manager.wait_idle(5)


def test_manager_never_captures_half_completed_business_method(monkeypatch):
    client = mongomock.MongoClient()
    written = threading.Event()
    original = client.tests.player.update_one

    def write(*args, **kwargs):
        written.set()
        return original(*args, **kwargs)

    with Manager(client=client, database="tests", batch_size=1) as manager:
        player = manager.get(Player, 1)
        manager.wait_idle(5)
        monkeypatch.setattr(client.tests.player, "update_one", write)
        entered, release = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(player.change_two_fields, entered, release)
            try:
                assert entered.wait(5)
                assert not written.wait(0.02)
            finally:
                release.set()
            job.result(5)
        manager.wait_idle(5)
        raw = client.tests.player.find_one({"_id": 1})
        assert raw["coins"] == raw["level"] == 1
