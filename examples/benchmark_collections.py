"""Compare array updates on a disposable DB; server timing requires an idle server."""

import argparse
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bson import BSON
from pymongo import MongoClient


def write_metrics(client):
    status = client.admin.command("serverStatus")
    return status["opLatencies"]["writes"]


def make_updates(strategy, size, batch_size, iterations):
    current = list(range(size))
    updates = []
    for revision in range(iterations):
        added = list(range(len(current), len(current) + batch_size))
        position = len(current)
        current.extend(added)
        update = {"$set": {"_orm_version": revision + 1, "_orm_write_token": str(revision)}}
        if strategy == "set_array":
            update["$set"]["values"] = current.copy()
        elif strategy == "set_indices":
            update["$set"].update(
                {f"values.{position + i}": value for i, value in enumerate(added)}
            )
        else:
            operator = "$push" if strategy == "push_each" else "$addToSet"
            update[operator] = {"values": {"$each": added}}
        updates.append(({"_id": 1, "_orm_version": revision}, update))
    return updates


def measure(client, collection, strategy, size, batch_size, iterations, workers):
    collection.delete_many({})
    collection.insert_many(
        {"_id": i, "_orm_version": 0, "values": list(range(size))} for i in range(1, workers + 1)
    )
    updates = make_updates(strategy, size, batch_size, iterations)
    payload_bytes = sum(len(BSON.encode({"q": query, "u": update})) for query, update in updates)
    before = write_metrics(client)
    started = time.perf_counter()

    def write_document(id):
        durations = 0.0
        for query, update in updates:
            started_write = time.perf_counter()
            result = collection.update_one({**query, "_id": id}, update)
            durations += time.perf_counter() - started_write
            assert result.matched_count == 1
        return durations

    if workers == 1:
        total_duration = write_document(1)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            total_duration = sum(executor.map(write_document, range(1, workers + 1)))
    elapsed = time.perf_counter() - started
    after = write_metrics(client)
    server_ops = after["ops"] - before["ops"]
    server_us = after["latency"] - before["latency"]
    for document in collection.find({}, {"values": 1}):
        assert document["values"] == list(range(size + batch_size * iterations))
    operation_count = iterations * workers
    return {
        "base_size": size,
        "batch_size": batch_size,
        "iterations": iterations,
        "workers": workers,
        "strategy": strategy,
        "mean_payload_bytes": round(payload_bytes / iterations),
        "mean_total_ms": round(total_duration * 1000 / operation_count, 4),
        "mean_server_ms": round(server_us / server_ops / 1000, 4) if server_ops else None,
        "operations_per_second": round(operation_count / elapsed, 2),
        "server_write_ops": server_ops,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default=os.environ.get("MONGODB_URI", "mongodb://localhost:27017"))
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 10000, 100000])
    parser.add_argument("--batches", type=int, nargs="+", default=[1, 32])
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "docs/benchmarks/collection-benchmark-results.json",
    )
    args = parser.parse_args()
    if (
        args.iterations < 1
        or args.workers < 1
        or any(size < 0 for size in args.sizes)
        or any(b < 1 for b in args.batches)
    ):
        parser.error("sizes must be nonnegative; batches, iterations and workers must be positive")

    client = MongoClient(args.uri, serverSelectionTimeoutMS=5000)
    database_name = "pyorm_benchmark_arrays_" + uuid.uuid4().hex
    results = []
    try:
        version = client.server_info()["version"]
        collection = client[database_name].arrays
        print(
            f"Workers: {args.workers}\n"
            "   size batch strategy       bytes/update  total ms  server ms    ops/sec",
            flush=True,
        )
        for size in args.sizes:
            for batch_size in args.batches:
                for strategy in ("set_array", "set_indices", "push_each", "add_to_set_each"):
                    result = measure(
                        client,
                        collection,
                        strategy,
                        size,
                        batch_size,
                        args.iterations,
                        args.workers,
                    )
                    results.append(result)
                    print(
                        f"{size:7d} {batch_size:5d} {strategy:16s} "
                        f"{result['mean_payload_bytes']:12d} {result['mean_total_ms']:9.4f} "
                        f"{result['mean_server_ms']:10.4f} {result['operations_per_second']:10.2f}",
                        flush=True,
                    )
        args.output.write_text(
            json.dumps(
                {
                    "mongodb_version": version,
                    "write_concern": {"w": 1},
                    "notes": (
                        "Integer arrays, one document per worker, prebuilt updates, "
                        "default journaling. "
                        "Payload excludes wire framing. Server latency is the global opLatencies "
                        "write delta: run against an idle, isolated server. This measures service "
                        "time and observed throughput at the selected concurrency, not a universal "
                        "database saturation limit."
                    ),
                    "results": results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Results: {args.output.resolve()}", flush=True)
    finally:
        assert database_name.startswith("pyorm_benchmark_arrays_")
        client.drop_database(database_name)
        client.close()


if __name__ == "__main__":
    main()
