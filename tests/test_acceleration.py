import importlib
import os
import random
import runpy
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path

import pytest
from bson import Binary, Code, DBRef, Decimal128, Int64, MaxKey, MinKey, ObjectId, Regex, Timestamp
from bson import encode as encode_bson

from pyorm import ValidationError
from pyorm._acceleration import native
from pyorm.serialization import (
    _collect_set_paths_python,
    _copy_bson_python,
    _encode_value_python,
    collect_set_paths,
    copy_bson,
    encode_value,
)


@pytest.mark.parametrize(
    ("mode", "failure", "expected_error"),
    [
        ("auto", ModuleNotFoundError(name="pyorm._native"), None),
        ("auto", ImportError("DLL load failed"), None),
        ("auto", ModuleNotFoundError(name="native_dependency"), ModuleNotFoundError),
        ("required", ModuleNotFoundError(name="pyorm._native"), ModuleNotFoundError),
        ("invalid", ImportError(), ValueError),
    ],
)
def test_backend_selection_preserves_visible_failures(
    monkeypatch, caplog, mode, failure, expected_error
):
    original = importlib.import_module

    def importing(name, *args, **kwargs):
        if name == "pyorm._native":
            raise failure
        return original(name, *args, **kwargs)

    monkeypatch.setenv("PYORM_NATIVE", mode)
    monkeypatch.setattr(importlib, "import_module", importing)
    path = Path(__file__).resolve().parents[1] / "pyorm/_acceleration.py"
    if expected_error is not None:
        with pytest.raises(expected_error):
            runpy.run_path(str(path))
    else:
        namespace = runpy.run_path(str(path))
        assert namespace["backend_name"] == "python"
        if isinstance(failure, ImportError) and not isinstance(failure, ModuleNotFoundError):
            assert "Native acceleration could not load" in caplog.text


def test_forced_python_mode_does_not_import_native_module():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, pyorm; from pyorm._acceleration import backend_name; "
            "assert backend_name == 'python'; assert 'pyorm._native' not in sys.modules",
        ],
        env={**os.environ, "PYORM_NATIVE": "off"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def accelerator():
    if native is None:
        pytest.skip("Native backend is disabled or unavailable")
    return native


def test_native_bson_sort_and_membership_match_driver_bytes(accelerator):
    values = [
        None,
        True,
        False,
        1,
        Int64(1),
        1.0,
        -0.0,
        float("nan"),
        "hello",
        "长字符串" * 90,
        b"bytes",
        Binary(b"old", subtype=2),
        Code("return n", {"n": [1, True]}),
        Code("code"),
        ObjectId(),
        Decimal128("1.2"),
        Regex("a", "i"),
        Timestamp(12, 1),
        datetime(2026, 1, 1, tzinfo=UTC),
        DBRef("items", 1),
        MinKey(),
        MaxKey(),
        {"a": 1, "b": True},
        [1, {"b": 2}],
    ]
    # Ordering crosses little-endian BSON length-prefix boundaries.
    values.extend("x" * size for size in (0, 1, 240, 248, 249, 250, 255, 256, 511, 512))
    rng = random.Random(421)
    for _ in range(100):
        old = rng.sample(values, rng.randrange(len(values)))
        new = rng.sample(values, rng.randrange(len(values)))
        ordered = accelerator.sort_set(encode_bson({"v": new}), new)
        expected = sorted(new, key=lambda item: encode_bson({"v": item}))
        assert encode_bson({"v": ordered}) == encode_bson({"v": expected})
        old_keys = {encode_bson({"v": item}) for item in old}
        new_members = {encode_bson({"v": item}): index for index, item in enumerate(new)}
        indices = accelerator.set_added(encode_bson({"v": old}), encode_bson({"v": new}))
        if old_keys - new_members.keys() or len(old_keys) != len(old):
            assert indices is None
        else:
            assert indices == [index for key, index in new_members.items() if key not in old_keys]
    assert accelerator.set_added(encode_bson({"v": []}), encode_bson({"v": ["a", "a"]})) == [1]


def test_native_copy_preserves_aliases_cycles_and_driver_leaves(accelerator):
    shared = [{"n": 1}]
    value = {"a": shared, "b": shared, "script": Code("return n", {"n": [1]})}
    copied = accelerator.copy_bson(value, {}, deepcopy)
    assert copied["a"] is copied["b"] and copied["a"] is not shared
    assert encode_bson(copied) == encode_bson(_copy_bson_python(value))
    value["script"].scope["n"].append(2)
    assert copied["script"].scope == {"n": [1]}
    cycle = []
    cycle.append(cycle)
    copied = accelerator.copy_bson(cycle, {}, deepcopy)
    assert copied is not cycle and copied[0] is copied
    deep = 1
    for _ in range(80):
        deep = [deep]
    assert copy_bson(deep) == _copy_bson_python(deep)


def test_native_copy_retains_python_callback_order(accelerator):
    calls = []

    class Leaf:
        def __init__(self, name):
            self.name = name

        def __deepcopy__(self, memo):
            calls.append(self.name)
            return Leaf(self.name)

    value = {Leaf("key"): Leaf("value")}
    deepcopy(value)
    expected = list(calls)
    calls.clear()
    accelerator.copy_bson(value, {}, deepcopy)
    assert calls == expected == ["value", "key"]


def test_native_conversion_and_set_paths_match_python(accelerator):
    class State(IntEnum):
        READY = 1

    class CustomList(list):
        def to_mongo(self):
            return {"custom": 2}

    value = {
        State.READY: [State.READY, CustomList([1]), {"tags": {"a", "b"}}],
        "nested": {1: {"tags": frozenset(("x", "y"))}},
        "tuple": (1, {"tags": {"z"}}),
    }
    assert encode_bson(encode_value(value)) == encode_bson(_encode_value_python(value))
    assert collect_set_paths(value, "data") == _collect_set_paths_python(value, "data")
    assert collect_set_paths(value, "data") == {"data.nested.1.tags"}
    unusual = {"\ud800": {"tags": {1}}, "unicode": {"标签": {2}}}
    assert collect_set_paths(unusual, "\ud801") == _collect_set_paths_python(unusual, "\ud801")


def test_native_collision_does_not_repeat_custom_converters(accelerator):
    calls = []

    class Converter:
        def to_mongo(self):
            calls.append(1)
            return 1

    with pytest.raises(ValidationError, match="collide"):
        encode_value({"first": Converter(), 1: 1, "1": 2})
    assert calls == [1]


def test_native_python_iteration_errors_remain_regular_exceptions(accelerator):
    for encoder in (_encode_value_python, encode_value):
        source = {}

        class Mutator:
            def __init__(self, target):
                self.target = target

            def to_mongo(self):
                self.target["new"] = 2
                return 1

        source["first"] = Mutator(source)
        with pytest.raises(RuntimeError, match="dictionary changed size"):
            encoder(source)

    for encoder in (_encode_value_python, encode_value):
        source = []

        class Appender:
            def __init__(self, target):
                self.target = target

            def to_mongo(self):
                self.target.append(2)
                return 1

        source.append(Appender(source))
        assert encoder(source) == [1, 2]


def test_native_rejects_truncated_envelopes_and_handles_corrupted_values(accelerator):
    data = encode_bson({"v": [1, "hello", {"x": True}]})
    for end in range(len(data)):
        with pytest.raises(ValueError):
            accelerator.sort_set(data[:end], [])
    rng = random.Random(311)
    for _ in range(500):
        damaged = bytearray(data)
        damaged[rng.randrange(11, len(data) - 2)] = rng.randrange(256)
        try:
            accelerator.set_added(data, bytes(damaged))
        except ValueError:
            pass  # Valid envelopes may contain invalid value boundaries; no panic is allowed.
