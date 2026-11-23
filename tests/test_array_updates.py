import threading
from copy import deepcopy

import mongomock
import pytest
from pymongo.errors import AutoReconnect

from pyorm import Document, EmbeddedDocument, EmbeddedField, Manager
from pyorm.serialization import diff_documents


class Bag(EmbeddedDocument):
    _autosave = ("items", "tags")
    items = []
    tags = set()


class Arrays(Document):
    __collection__ = "arrays"
    _autosave = ("items", "tags", "data")
    items = []
    tags = set()
    data = {"items": []}
    bag = EmbeddedField(Bag, default_factory=Bag, db_field="bag_data")


def test_tail_additions_coalesce_including_nested_and_embedded_arrays(monkeypatch):
    client = mongomock.MongoClient()
    collection = client.tests.arrays
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        document = manager.get(Arrays, 1)
        document.items.append("old")
        document.tags.add("old")
        manager.wait_idle(5)
        original = collection.update_one
        writes = []

        def write(query, update, **kwargs):
            writes.append(deepcopy(update))
            return original(query, update, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(collection, "update_one", write)
            document.items.append({"count": 1})
            document.items[-1]["count"] = 2
            document.items.extend(["a", "b"])
            document.tags.update({"a", "b", "old"})
            document.tags.add("a")
            document.data["items"].extend([1, 2])
            document.bag.items.append(3)
            document.bag.tags.add("rare")
            manager.wait_idle(5)

        assert len(writes) == 1
        assert writes[0]["$push"] == {
            "items": {"$each": [{"count": 2}, "a", "b"]},
            "tags": {"$each": ["a", "b"]},
            "data.items": {"$each": [1, 2]},
        }
        business_set = {k: v for k, v in writes[0]["$set"].items() if not k.startswith("_orm_")}
        assert business_set == {"bag_data.items.0": 3, "bag_data.tags.0": "rare"}
        raw = collection.find_one({"_id": 1})
        assert raw["items"] == ["old", {"count": 2}, "a", "b"]
        assert raw["tags"] == ["old", "a", "b"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ([1], [True, 2]),
        ([1], [1.0, 2]),
        ([{"n": 1}], [{"n": True}, 2]),
        ([1, 2], [0, 1, 2]),
        ([1, 2], [1]),
        ([1, 2], [2, 1]),
    ],
)
def test_changed_prefix_removal_or_reorder_replaces_array(old, new):
    update = diff_documents({"items": old}, {"items": new}, incremental_arrays=True)
    assert update == {"$set": {"items": new}}


@pytest.mark.parametrize("new_tags", [{"old"}, {"new", "old"}])
def test_set_removal_or_mixed_change_replaces_array(new_tags):
    update = diff_documents(
        {"tags": ["gone", "old"]},
        {"tags": sorted(new_tags)},
        incremental_arrays=True,
        set_paths={"tags"},
    )
    assert update == {"$set": {"tags": sorted(new_tags)}}


def test_set_storage_order_and_repeated_add_do_not_trigger_rewrites(monkeypatch):
    client = mongomock.MongoClient()
    collection = client.tests.arrays
    collection.insert_one({"_id": 1, "_orm_version": 1, "tags": ["z", "a"]})
    with Manager(client=client, database="tests", flush_interval=60) as manager:
        document = manager.get(Arrays, 1)
        manager.wait_idle(5)  # Fill defaults absent in this legacy document.
        original = collection.update_one
        writes = []

        def write(query, update, **kwargs):
            writes.append(deepcopy(update))
            return original(query, update, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(collection, "update_one", write)
            document.tags.add("b")
            manager.wait_idle(5)
            assert writes[0]["$set"]["tags.2"] == "b"
            revision = document.version
            document.tags.add("a")
            manager.wait_idle(5)
            assert len(writes) == 1 and document.version == revision
        assert collection.find_one({"_id": 1})["tags"] == ["z", "a", "b"]


@pytest.mark.parametrize("lost_after_write", [False, True])
@pytest.mark.parametrize("batch", [1, 2])
def test_retry_never_duplicates_list_or_set_additions(monkeypatch, lost_after_write, batch):
    client = mongomock.MongoClient()
    collection = client.tests.arrays
    with Manager(
        client=client,
        database="tests",
        flush_interval=60,
        retry_interval=0.01,
        on_error=lambda *_: None,
    ) as manager:
        document = manager.get(Arrays, 1)
        document.items.append("old")
        document.tags.add("old")
        manager.wait_idle(5)
        original = collection.update_one
        writes = []

        def write(query, update, **kwargs):
            writes.append((deepcopy(query), deepcopy(update)))
            if len(writes) == 1:
                if lost_after_write:
                    original(query, update, **kwargs)
                raise AutoReconnect("lost acknowledgement")
            return original(query, update, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(collection, "update_one", write)
            document.items.extend(list(range(batch)))
            document.tags.update({str(i) for i in range(batch)})
            manager.wait_idle(5)
        raw = collection.find_one({"_id": 1})
        assert raw["items"] == ["old", *range(batch)]
        assert len(raw["tags"]) == 1 + batch and set(raw["tags"]) == document.tags
        assert raw["_orm_version"] == document.version == 2
        assert writes[0] == writes[1]
        if lost_after_write and batch > 1:
            assert len(writes) == 3 and "$push" not in writes[-1][1]
        else:
            assert len(writes) == 2


def test_append_during_inflight_retry_keeps_new_cache_and_next_batch(monkeypatch):
    client = mongomock.MongoClient()
    collection = client.tests.arrays
    with Manager(
        client=client,
        database="tests",
        flush_interval=60,
        retry_interval=0.01,
        on_error=lambda *_: None,
    ) as manager:
        document = manager.get(Arrays, 1)
        manager.wait_idle(5)
        original = collection.update_one
        entered, release = threading.Event(), threading.Event()
        writes = []

        def write(query, update, **kwargs):
            writes.append(deepcopy(update))
            result = original(query, update, **kwargs)
            if len(writes) == 1:
                entered.set()
                assert release.wait(5)
                raise AutoReconnect("server appended, acknowledgement lost")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(collection, "update_one", write)
            manager.batch_size = 1
            document.items.extend([{"count": 1}, "b"])
            try:
                assert entered.wait(5)
                document.items[0]["count"] = 2
                document.items.append("c")
                assert document.items == [{"count": 2}, "b", "c"]
                assert manager.get(Arrays, 1) is document
            finally:
                release.set()
            manager.wait_idle(5)
        assert writes[0]["$push"] == {"items": {"$each": [{"count": 1}, "b"]}}
        assert writes[-1]["$set"]["items"] == [{"count": 2}, "b", "c"]
        assert document.items == collection.find_one({"_id": 1})["items"]
        assert document.version == 3 and not document.is_dirty
