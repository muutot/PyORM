import mongomock
import pytest

from pyorm import Document, EmbeddedDocument, EmbeddedField, Manager, ValidationError


class ContainerDetails(EmbeddedDocument):
    _autosave = ("items", "mapping", "coords", "tags")
    items = []
    mapping = {"bonus": {"attack": 0, "old": 1}}
    coords = (0, 0)
    tags = set()


class Containers(Document):
    __collection__ = "containers"
    _autosave = ("items", "mapping", "coords", "tags")
    items = []
    mapping = {"bonus": {"attack": 0, "old": 1}}
    coords = (0, 0)
    tags = set()
    details = EmbeddedField(ContainerDetails, default_factory=ContainerDetails)


@pytest.mark.parametrize("embedded", [False, True])
def test_container_types_and_incremental_mutations_survive_fresh_load(embedded, monkeypatch):
    client = mongomock.MongoClient()
    collection = client.tests.containers
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        document = manager.get(Containers, 1)
        target = document.details if embedded else document
        target.items.append({"quantity": 1})
        target.tags.add("rare")
        target.coords = (10, 20)
        manager.wait_idle(5)

        writes = []
        original = collection.update_one

        def write(query, update, **kwargs):
            writes.append(update)
            return original(query, update, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(collection, "update_one", write)
            target.items[0]["quantity"] = 2
            target.mapping["bonus"]["attack"] = 25
            del target.mapping["bonus"]["old"]
            target.coords += (30,)
            target.tags |= {"trained"}
            manager.wait_idle(5)

        assert len(writes) == 1
        prefix = "details." if embedded else ""
        business_set = {k: v for k, v in writes[0]["$set"].items() if not k.startswith("_orm_")}
        assert business_set == {
            prefix + "items": [{"quantity": 2}],
            prefix + "mapping.bonus.attack": 25,
            prefix + "coords.2": 30,
            prefix + "tags.1": "trained",
        }
        assert writes[0]["$unset"] == {prefix + "mapping.bonus.old": ""}

    with Manager(client=client, database="tests", flush_interval=60) as manager:
        restored = manager.get(Containers, 1)
        target = restored.details if embedded else restored
        assert isinstance(target.items, list) and target.items == [{"quantity": 2}]
        assert isinstance(target.mapping, dict) and target.mapping == {"bonus": {"attack": 25}}
        assert isinstance(target.coords, tuple) and target.coords == (10, 20, 30)
        assert isinstance(target.tags, set) and target.tags == {"rare", "trained"}
        assert manager.get(Containers, 1) is restored
        target.items.append({"quantity": 3})
        target.mapping["bonus"]["attack"] += 5
        target.coords = (40, 50)
        target.tags.discard("rare")

    raw = collection.find_one({"_id": 1})
    payload = raw["details"] if embedded else raw
    assert payload["items"] == [{"quantity": 2}, {"quantity": 3}]
    assert payload["mapping"] == {"bonus": {"attack": 30}}
    assert payload["coords"] == [40, 50] and payload["tags"] == ["trained"]


def test_container_defaults_are_independent_and_empty_types_survive_load():
    client = mongomock.MongoClient()
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        first = manager.get(Containers, 1)
        second = manager.get(Containers, 2)
        first.items.append(1)
        first.mapping["bonus"]["attack"] = 10
        first.tags.add("rare")
        first.details.items.append(2)
        assert second.items == [] and second.tags == set()
        assert second.mapping["bonus"]["attack"] == 0
        assert second.details.items == [] and first.details.items == [2]

    with Manager(client=client, database="tests") as manager:
        second = manager.get(Containers, 2)
        assert isinstance(second.items, list) and second.items == []
        assert isinstance(second.mapping, dict)
        assert isinstance(second.coords, tuple) and second.coords == (0, 0)
        assert isinstance(second.tags, set) and second.tags == set()


def test_augmented_assignment_preserves_live_container_references():
    client = mongomock.MongoClient()
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        document = manager.get(Containers, 1)
        items, tags, mapping = document.items, document.tags, document.mapping
        document.items += [1]
        document.tags |= {"rare"}
        document.mapping |= {"extra": 1}
        assert document.items is items and document.tags is tags and document.mapping is mapping
        items.append(2)
        tags.add("trained")
        mapping["extra"] = 2

    raw = client.tests.containers.find_one({"_id": 1})
    assert raw["items"] == [1, 2] and set(raw["tags"]) == {"rare", "trained"}
    assert raw["mapping"]["extra"] == 2


def test_tuple_tracks_nested_mutable_members_and_set_restores_tuple_members():
    class Mixed(Document):
        _autosave = ("payload", "points")
        payload = ({"items": []},)
        points = set()

    client = mongomock.MongoClient()
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        document = manager.get(Mixed, 1)
        document.points.add((1, 2))
        manager.wait_idle(5)
        document.payload[0]["items"].append(7)

    with Manager(client=client, database="tests") as manager:
        restored = manager.get(Mixed, 1)
        assert isinstance(restored.payload, tuple) and restored.payload == ({"items": [7]},)
        assert restored.points == {(1, 2)}
        restored.payload[0]["items"].append(8)
        restored.points.add((3, 4))

    raw = client.tests.mixed.find_one({"_id": 1})
    assert raw["payload"] == [{"items": [7, 8]}]
    assert {tuple(point) for point in raw["points"]} == {(1, 2), (3, 4)}


def test_declared_container_type_rejects_incompatible_assignment_before_cache_change():
    client = mongomock.MongoClient()
    with Manager(client=client, database="tests") as manager:
        document = manager.get(Containers, 1)
        original = document.tags
        with pytest.raises(ValidationError):
            document.tags = ["rare"]
        assert document.tags is original and document.tags == set()
