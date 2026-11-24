from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

import mongomock
import pytest

from pyorm import Document, EmbeddedDocument, EmbeddedField, Field, Manager, ValidationError


class Stats(EmbeddedDocument):
    _autosave = ("coins",)
    coins = 0


class Account(Document[int]):
    __collection__ = "accounts"
    _autosave = ("coins",)
    coins = 0
    stats = EmbeddedField(Stats)


def test_autosave_scalar_rejects_invalid_assignment():
    account = Account()
    with pytest.raises(ValidationError):
        account.coins = "corrupt"


def test_autosave_embedded_scalar_rejects_invalid_assignment():
    account = Account()
    with pytest.raises(ValidationError):
        account.stats.coins = "corrupt"


@pytest.mark.parametrize("value", [{"coins": "corrupt"}, {"stats": {"coins": "corrupt"}}])
def test_autosave_scalar_rejects_invalid_loaded_data(value):
    client = mongomock.MongoClient()
    client.tests.accounts.insert_one({"_id": 1, **value})
    with Manager(client=client, database="tests") as manager:
        with pytest.raises(ValidationError):
            manager.get(Account, 1)
        assert manager.stats["cached"] == 0


@pytest.mark.parametrize(
    ("default", "invalid"),
    [(0, True), (0, 0.5), (False, 1), (0.0, 1), (0.0, True), ("", 1)],
)
def test_default_inference_uses_existing_field_numeric_rules(default, invalid):
    class Value(Document):
        _autosave = ("value",)
        value = default

    document = Value()
    with pytest.raises(ValidationError):
        document.value = invalid
    assert type(document.value) is type(default) and document.value == default


def test_annotations_control_optional_values_and_numeric_unions():
    class Value(Document):
        _autosave = ("coins", "amount", "strict", "either")
        coins: int | None = None
        amount: Annotated[int | float, "numeric"] = 0
        strict: int = 0
        either: int | bool = 0

    value = Value()
    value.coins = 1
    value.coins = None
    value.amount = 0.5
    value.amount = 1
    value.either = True
    with pytest.raises(ValidationError):
        value.coins = "corrupt"
    with pytest.raises(ValidationError):
        value.coins = True
    with pytest.raises(ValidationError):
        value.amount = True
    with pytest.raises(ValidationError):
        value.strict = None


def test_unannotated_null_defaults_and_explicit_dynamic_fields_remain_flexible():
    class Value(Document):
        _autosave = ("unknown", "legacy", "dynamic", "explicit")
        unknown = None
        legacy = 0
        dynamic: Any = 0
        explicit: int = Field(default=0)

    value = Value()
    value.unknown = "unknown type"
    value.legacy = None
    value.dynamic = "dynamic type"
    value.explicit = "explicitly untyped"
    assert value.to_mongo()["dynamic"] == "dynamic type"


def test_optional_container_annotation_restores_top_level_type():
    class Value(Document):
        _autosave = ("tags",)
        tags: set[str] | None = None

    value = Value.from_mongo({"_id": 1, "tags": ["rare", "trained"]})
    assert value.tags == {"rare", "trained"}
    with pytest.raises(ValidationError):
        value.tags = ["rare"]


@pytest.mark.parametrize("explicit", [False, True])
def test_embedded_instance_defaults_clone_without_copying_locks(explicit):
    default = Stats(coins=3)

    class Value(Document):
        _autosave = ("stats",)
        stats = EmbeddedField(Stats, default=default) if explicit else default

    left, right = Value(), Value()
    assert left.stats is not right.stats and left.stats is not default
    left.stats.coins = 4
    assert right.stats.coins == default.coins == 3
    loaded = Value.from_mongo({"_id": 1, "stats": {"coins": 5}})
    assert isinstance(loaded.stats, Stats) and loaded.stats.coins == 5


def test_local_forward_annotation_restores_embedded_document():
    class LocalStats(EmbeddedDocument):
        _autosave = ("score",)
        score = 0

    class Value(Document):
        _autosave = ("stats",)
        stats: LocalStats | None = None

    value = Value.from_mongo({"_id": 1, "stats": {"score": 3}})
    assert isinstance(value.stats, LocalStats) and value.stats.score == 3
    with pytest.raises(ValidationError):
        value.stats.score = "corrupt"


def test_enum_default_infers_restoration_type():
    class Color(Enum):
        RED = 1

    class Value(Document):
        _autosave = ("color",)
        color = Color.RED

    loaded = Value.from_mongo({"_id": 1, "color": "RED"})
    assert loaded.color is Color.RED


def test_explicit_numeric_union_rejects_bool_unless_declared():
    class Value(Document):
        number = Field((int, float), default=0)
        either = Field((int, bool), default=0)

    value = Value()
    with pytest.raises(ValidationError):
        value.number = True
    value.either = True


def test_embedded_template_copy_preserves_python_values_and_does_not_run_factories():
    calls = []

    def factory():
        calls.append(1)
        return {}

    class RichStats(EmbeddedDocument):
        payload = Field(dict, default_factory=factory)

    shared = []
    template = RichStats(
        payload={"coords": (1, 2), "tags": {"rare"}, "left": shared, "right": shared}
    )

    class Value(Document):
        stats = EmbeddedField(RichStats, default=template)

    left, right = Value(), Value()
    assert not calls
    assert isinstance(left.stats.payload["coords"], tuple)
    assert isinstance(left.stats.payload["tags"], set)
    assert left.stats.payload["left"] is left.stats.payload["right"]
    left.stats.payload["left"].append(1)
    assert right.stats.payload["left"] == template.payload["left"] == []


def test_default_copy_detaches_bound_containers_and_embedded_tracking():
    class Owned(Document):
        _autosave = ("items",)
        items = [{"values": []}]
        stats = EmbeddedField(Stats)

    with Manager(client=mongomock.MongoClient(), database="tests", flush_interval=60) as manager:
        source = manager.get(Owned, 1)

        class Copy(Document):
            _autosave = ("items",)
            items = source.items
            stats = EmbeddedField(Stats, default=source.stats)

        copy = Copy()
        copy.items[0]["values"].append(1)
        copy.stats.coins = 2
        assert source.items == [{"values": []}] and source.stats.coins == 0


@pytest.mark.parametrize("annotation", [set | tuple, set | int, frozenset | list])
def test_restoration_unions_require_an_explicit_codec(annotation):
    with pytest.raises(TypeError, match="restoration"):

        class Value(Document):
            _autosave = ("value",)
            value: annotation = set()


def test_enum_union_requires_an_explicit_codec():
    class Color(Enum):
        RED = 1

    with pytest.raises(TypeError, match="restoration"):

        class Value(Document):
            _autosave = ("value",)
            value: Color | int = 0
