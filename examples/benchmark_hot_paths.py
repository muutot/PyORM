"""Measure cached operations, isolated packet capture, and real MongoDB drains."""

import argparse
import json
import os
import platform
import statistics
import time
import tracemalloc
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pymongo import MongoClient

import pyorm
from pyorm import Document, Field, Manager


class Entry(Document[int]):
    __collection__ = "entries"
    value = Field(int, default=0)


class ContainerEntry(Document[int]):
    __collection__ = "containers"
    items = Field(list, default_factory=list)
    data = Field(dict, default_factory=dict)
    tags = Field(set, default_factory=set)


def timed(action):
    started = time.perf_counter()
    action()
    return time.perf_counter() - started


def measure_cache(client, database, args):
    collection = client[database].entries
    collection.delete_many({})
    collection.insert_many({"_id": i, "value": 0, "_orm_version": 1} for i in range(args.documents))
    result = {}
    with Manager(
        client=client,
        database=database,
        workers=args.workers,
        flush_interval=600,
        batch_size=args.documents + 1,
        max_pending=args.workers * 4,
    ) as manager:
        started = time.perf_counter()
        entries = [manager.get(Entry, i) for i in range(args.documents)]
        result["load_seconds"] = time.perf_counter() - started

        def hits():
            for _ in range(args.mutations):
                for entry in entries:
                    assert manager.get(Entry, entry.id) is entry

        def assign(partition):
            for value in range(1, args.mutations + 1):
                for entry in partition:
                    entry.value = value

        result["cache_hit_seconds"] = timed(hits)
        result["assignment_seconds"] = timed(lambda: assign(entries))
        result["drain_seconds"] = timed(lambda: manager.wait_idle(60))
        # Independent partitions measure scheduling contention between documents.
        partitions = [entries[i :: args.workers] for i in range(args.workers)]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            result["concurrent_assignment_seconds"] = timed(
                lambda: list(pool.map(assign, partitions))
            )
        manager.wait_idle(60)
    assert collection.count_documents({"value": args.mutations}) == args.documents
    assert collection.count_documents({"_orm_version": 2}) == args.documents
    return result


def measure_container(client, database, args, kind):
    collection = client[database].containers
    payload = {"items": [], "data": {}, "tags": []}
    if kind == "list":
        payload["items"] = list(range(args.size))
    elif kind == "dict":
        payload["data"] = {str(i): i for i in range(args.size // 10)}
    else:
        payload["tags"] = list(range(args.size // 10))
    collection.delete_many({})
    collection.insert_one({"_id": 1, "_orm_version": 1, **payload})
    with Manager(
        client=client, database=database, flush_interval=600, batch_size=100000
    ) as manager:
        started = time.perf_counter()
        entry = manager.get(ContainerEntry, 1)
        load = time.perf_counter() - started
        if kind == "list":
            entry.items.extend([args.size, args.size + 1])
            expected = {"$push": {"items": {"$each": [args.size, args.size + 1]}}}
        elif kind == "dict":
            entry.data["0"] = -1
            expected = {"$set": {"data.0": -1}}
        else:
            entry.tags.add(args.size)
            expected = {"$set": {f"tags.{args.size // 10}": args.size}}
        # Internal diagnostic: excludes network I/O and leaves the business cache intact.
        elapsed = timed(lambda: [manager._capture(entry) for _ in range(args.captures)])
        tracemalloc.start()
        try:
            packet = manager._capture(entry)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert packet.update == expected
        drain = timed(lambda: manager.wait_idle(60))
        raw = collection.find_one({"_id": 1})
        assert raw["_orm_version"] == entry.version == 2
        assert raw["items"] == entry.items and raw["data"] == entry.data
        assert set(raw["tags"]) == entry.tags and not entry.is_dirty
    return {
        "load_seconds": load,
        "capture_ms": elapsed * 1000 / args.captures,
        "capture_peak_bytes": peak,
        "drain_ms": drain * 1000,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default=os.environ.get("MONGODB_URI", "mongodb://localhost:27017"))
    parser.add_argument("--documents", type=int, default=1000)
    parser.add_argument("--mutations", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--size", type=int, default=100000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--captures", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.documents, args.mutations, args.workers, args.rounds, args.captures) < 1:
        parser.error("counts must be positive")
    if args.size < 10:
        parser.error("size must be at least 10")
    database = "pyorm_benchmark_hot_paths_" + uuid.uuid4().hex
    samples = {name: [] for name in ("cache", "list", "dict", "set")}
    with MongoClient(args.uri, serverSelectionTimeoutMS=5000) as client:
        try:
            version = client.server_info()["version"]
            for repeat in range(args.rounds):
                samples["cache"].append(measure_cache(client, database, args))
                for kind in ("list", "dict", "set"):
                    samples[kind].append(measure_container(client, database, args, kind))
                print(f"Round {repeat + 1}/{args.rounds} complete", flush=True)
        finally:
            assert database.startswith("pyorm_benchmark_hot_paths_")
            client.drop_database(database)
    medians = {
        kind: {key: statistics.median(row[key] for row in rows) for key in rows[0]}
        for kind, rows in samples.items()
    }
    result = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "mongodb": version,
        "implementation_path": str(Path(pyorm.__file__).resolve().parent),
        "write_concern": {"w": 1},
        "workload": {
            key: value for key, value in vars(args).items() if key not in ("uri", "output")
        },
        "notes": (
            "Disposable database; default journaling; medians of independent rounds. "
            "List size is --size, dict/set size is --size/10. Capture excludes MongoDB I/O; "
            "tracemalloc is measured separately from timings. Drain includes capture and "
            "acknowledged writes, with cache queue capacity limited to workers*4. "
            "Concurrent assignments use disjoint documents and repeat the existing values."
        ),
        "medians": medians,
        "samples": samples,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(medians, indent=2), flush=True)


if __name__ == "__main__":
    main()
