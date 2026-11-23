from concurrent.futures import ThreadPoolExecutor
from enum import Enum

import pytest
from pymongo.errors import AutoReconnect, DuplicateKeyError

from pyorm import (
    ConcurrentModificationError,
    Document,
    EmbeddedDocument,
    EmbeddedField,
    Field,
    ListField,
    ValidationError,
)

from .conftest import Player


def test_create_load_nested_diff_and_delete(players):
    player = players.create(id=0, name="Ada", inventory={"bag": {"count": 1, "old": 2}})
    loaded = players.get(0)
    assert loaded.name == "Ada" and loaded.version == 1 and not loaded.is_dirty
    loaded.inventory["bag"]["count"] = 3
    del loaded.inventory["bag"]["old"]
    loaded.tags.add("vip")
    update = loaded.pending_update()
    assert update["$set"]["inventory.bag.count"] == 3
    assert "inventory.bag.old" in update["$unset"]
    loaded.save()
    assert loaded.version == 2 and not loaded.is_dirty
    loaded.save()
    assert loaded.version == 2
    assert players.get(0).inventory == {"bag": {"count": 3}}
    assert loaded.delete() and players.get(0) is None
    assert not loaded.delete()
    with pytest.raises(RuntimeError, match="Deleted"):
        loaded.save()
    assert player.version == 1


def test_conflicting_instances_and_atomic_updates(players):
    players.create(id=1, name="Ada")
    left, right = players.get(1), players.get(1)
    left.coins = 10
    left.save()
    right.coins = 20
    with pytest.raises(ConcurrentModificationError):
        right.save()
    assert right.is_dirty and right.version == 1
    right.reload()
    assert right.coins == 10 and not right.is_dirty
    players.filter(id=1).update(inc__coins=5)
    right.coins = 30
    with pytest.raises(ConcurrentModificationError):
        right.save()
    with pytest.raises(ConcurrentModificationError):
        right.delete()
    assert players.get(1).coins == 15


def test_failed_save_keeps_dirty_state(players, monkeypatch):
    player = players.create(id=1, name="Ada")
    player.coins = 7
    collection = players.orm._collection(Player)

    def fail(*args, **kwargs):
        raise AutoReconnect("uncertain network result")

    with monkeypatch.context() as patch:
        patch.setattr(collection, "update_one", fail)
        with pytest.raises(AutoReconnect):
            player.save()
    assert player.is_dirty and player.version == 1
    player.save()
    assert players.get(1).coins == 7


def test_duplicate_insert_is_visible(players):
    players.create(id=1, name="Ada")
    another = players.new(id=1, name="Other")
    with pytest.raises(DuplicateKeyError):
        another.save()
    assert not another.is_persisted


def test_defaults_validation_and_optional_removal(players):
    left, right = players.new(name="A"), players.new(name="B")
    left.inventory["x"] = 1
    assert right.inventory == {}
    with pytest.raises(ValidationError):
        players.create()
    with pytest.raises(ValidationError):
        left.coins = True
    with pytest.raises(ValidationError):
        left.name = None
    with pytest.raises(ValidationError):
        players.new(unknown=1)
    left.save()
    del left.coins
    left.save()
    assert "coins" not in players.filter(id=left.id).raw()[0]
    with pytest.raises(ValidationError):
        del left.name


def test_arrays_replace_as_whole_without_index_conflicts(players):
    player = players.create(name="A", inventory={"items": [{"x": 1}, {"x": 1}]})
    player.inventory["items"].pop(0)
    player.inventory["items"][0]["x"] = 2
    assert player.pending_update() == {"$set": {"inventory.items": [{"x": 2}]}}
    player.save()
    assert players.get(player.id).inventory["items"] == [{"x": 2}]


def test_array_type_changes_are_not_hidden_by_python_equality(players):
    player = players.create(name="A", inventory={"items": [1, {"nested": 2}]})
    player.inventory["items"][0] = True
    player.inventory["items"][1]["nested"] = 2.0
    assert "inventory.items" in player.pending_update()["$set"]
    player.save()
    items = players.get(player.id).inventory["items"]
    assert type(items[0]) is bool and type(items[1]["nested"]) is float


def test_embedded_enum_and_containers(orm):
    class Color(Enum):
        RED = 1
        BLUE = 2

    class Stats(EmbeddedDocument):
        score = Field(int, default=0)

    class Item(Document):
        color = Field(Color, default=Color.RED)
        stats = EmbeddedField(Stats, default_factory=Stats)
        history = ListField(EmbeddedField(Stats))
        points = Field(set, default_factory=set)
        coords = Field(tuple, default=(1, 2))

    item = orm[Item].create(history=[Stats(score=3)], points={(1, 2), (3, 4)})
    loaded = orm[Item].get(item.id)
    assert loaded.color is Color.RED
    assert loaded.history[0].score == 3
    assert loaded.points == {(1, 2), (3, 4)} and loaded.coords == (1, 2)
    loaded.stats.score = 5
    assert loaded.pending_update() == {"$set": {"stats.score": 5}}
    loaded.save()
    assert orm[Item].get(item.id).stats.score == 5


def test_embedded_field_infers_independent_defaults_and_respects_overrides():
    class Stats(EmbeddedDocument):
        score = Field(int, default=0)

    class Item(Document):
        stats = EmbeddedField(Stats)
        custom = EmbeddedField(Stats, default_factory=lambda: Stats(score=7))
        optional = EmbeddedField(Stats, default=None)
        unset = EmbeddedField(Stats, default_factory=None)

    left, right = Item(), Item()
    assert isinstance(left.stats, Stats) and left.stats.score == 0
    assert left.stats is not right.stats
    left.stats.score = 5
    assert right.stats.score == 0
    assert left.custom.score == 7 and left.optional is None
    assert "unset" not in left.to_mongo()


@pytest.mark.parametrize("key", ["a.b", "$bad", "", "a\0b", False])
def test_invalid_dict_keys(players, key):
    with pytest.raises(ValidationError):
        players.create(name="A", inventory={key: 1})


def test_colliding_keys_and_bson_errors(players):
    with pytest.raises(ValidationError, match="collide"):
        players.create(name="A", inventory={1: 2, "1": 3})
    with pytest.raises(ValidationError):
        players.create(name="A", inventory={"x": object()})
    with pytest.raises(ValidationError):
        players.create(name="A", coins=2**100)


def test_existing_server_documents_preserve_unknown_fields(players):
    players.orm.client.tests.players.insert_one(
        {
            "_id": 12,
            "name": "Old",
            "coins": 3,
            "inventory": {},
            "tags": [],
            "unmapped": {"still": "here"},
        }
    )
    player = players.get(12)
    assert player.version == 0 and not player.is_dirty
    player.coins = 4
    player.save()
    raw = players.filter(id=12).raw()[0]
    assert raw["unmapped"] == {"still": "here"} and raw["_orm_version"] == 1


def test_legacy_declarations_and_threaded_construction(orm):
    class Legacy(Document):
        _collection = "legacy"
        _autosave = ("name", "items")
        name = ""
        items = []

    repo = orm[Legacy]
    with ThreadPoolExecutor(max_workers=16) as executor:
        items = list(executor.map(lambda i: repo.new(id=i, name=str(i)), range(200)))
    assert [item.name for item in items] == [str(i) for i in range(200)]
    items[0].items.append(1)
    assert items[1].items == []
    items[0].save()
    assert repo.get(0).items == [1]


def test_model_field_alias_and_reserved_fields(orm):
    class Alias(Document):
        name = Field(str, db_field="playerName")

    orm[Alias].create(name="A")
    assert orm[Alias].filter(name="A").first().name == "A"
    orm[Alias].filter(name="A").update(name="B")
    assert orm[Alias].filter(name="B").count() == 1
    with pytest.raises(TypeError):

        class Bad(Document):
            x = Field(db_field="_orm_version")

    with pytest.raises(TypeError):

        class BadMethod(Document):
            save = Field()
