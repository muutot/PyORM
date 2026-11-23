import pytest
from pymongo import ASCENDING, IndexModel
from pymongo.errors import DuplicateKeyError

from pyorm import Document, Field, ValidationError


def test_filters_sort_pagination_and_raw_projection(players):
    for i in range(6):
        players.create(id=i, name=f"p{i}", coins=i)
    base = players.filter(coins__gte=2)
    query = base.filter(coins__lt=5).sort("-coins").skip(1).limit(2)
    assert [row.id for row in query] == [3, 2]
    assert query.count() == 2 and base.count() == 4
    assert query.exists() and not players.filter(name="absent").exists()
    assert players.filter(id__in=[0, 4]).count() == 2
    assert players.filter(coins__gte=2, coins__lte=2).first().id == 2
    assert set(players.get_many([1, 4, 100])) == {1, 4}
    projected = players.filter(id=1).raw({"name": 1, "_id": 0})
    assert projected == [{"name": "p1"}]
    assert players.get(100) is None


def test_filter_arguments_are_snapshots(players):
    players.create(id=1, name="A")
    original = {"_id": {"$in": [1]}}
    query = players.filter(original)
    original["_id"]["$in"].clear()
    assert query.count() == 1


def test_atomic_update_returns_model_and_upsert(players):
    assert players.update_one({"_id": 1}, inc__coins=1) is None
    row = players.update_one({"_id": 1}, upsert=True, name="A", inc__coins=5)
    assert row.coins == 5 and row.version == 1
    row = players.update_one({"_id": 1}, inc__coins=2)
    assert row.coins == 7 and row.version == 2
    row.coins = 8
    row.save()
    assert players.get(1).coins == 8


def test_aggregate_and_bulk_delete(players):
    players.create(name="A", coins=3)
    players.create(name="B", coins=7)
    assert players.aggregate([{"$group": {"_id": None, "total": {"$sum": "$coins"}}}]) == [
        {"_id": None, "total": 10}
    ]
    assert players.filter(coins__lt=5).delete().deleted_count == 1
    assert players.filter().count() == 1


@pytest.mark.parametrize(
    "update",
    [
        {"$set": {"_orm_version": 0}},
        {"$unset": {"_id": 1}},
        {"$set": {"_id.x": 1}},
        {"$rename": {"name": "_orm_version"}},
        {"$foo": {"coins": 1}},
        {},
        {"$set": {}},
        {"$set": {"bad..path": 1}},
    ],
)
def test_reserved_and_invalid_updates(players, update):
    with pytest.raises(ValidationError):
        players.filter().update(update)


def test_paginated_writes_rejected(players):
    with pytest.raises(ValueError):
        players.filter().limit(1).delete()
    with pytest.raises(ValueError):
        players.filter().sort("name").update(coins=1)
    with pytest.raises(ValueError):
        players.filter().skip(-1)


def test_unique_indexes(orm):
    class User(Document):
        email = Field(str, required=True)
        __indexes__ = (IndexModel([("email", ASCENDING)], unique=True),)

    repo = orm[User]
    assert repo.ensure_indexes()
    repo.create(email="a@example.com")
    with pytest.raises(DuplicateKeyError):
        repo.create(email="a@example.com")
