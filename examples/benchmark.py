"""Measure automatic Manager persistence and coalescing in a disposable database."""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from pymongo import MongoClient

from pyorm import Document, Manager


class Entry(Document):
    __collection__ = "entries"
    _autosave = ("value",)
    value = 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operations", type=int, default=1000, help="Cached document count")
    parser.add_argument("--threads", type=int, default=8, help="MongoDB worker count")
    parser.add_argument("--mutations", type=int, default=100, help="Assignments per document")
    parser.add_argument("--output")
    args = parser.parse_args()
    if min(args.operations, args.threads, args.mutations) < 1:
        parser.error("operations, threads and mutations must be positive")
    uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
    database = "pyorm_benchmark_" + uuid.uuid4().hex
    results = {
        "python": sys.version.split()[0],
        "documents": args.operations,
        "assignments": args.operations * args.mutations,
        "workers": args.threads,
    }
    with MongoClient(uri, serverSelectionTimeoutMS=5000) as client:
        try:
            collection = client[database].entries
            collection.insert_many(
                [{"_id": i, "value": 0, "_orm_version": 1} for i in range(args.operations)]
            )
            with Manager(
                client=client,
                database=database,
                workers=args.threads,
                max_pending=max(1024, args.operations),
                flush_interval=60,
                batch_size=args.operations + 1,
            ) as manager:
                started = time.perf_counter()
                entries = [manager.get(Entry, i) for i in range(args.operations)]
                results["initial_load_seconds"] = round(time.perf_counter() - started, 4)

                started = time.perf_counter()
                for value in range(1, args.mutations + 1):
                    for entry in entries:
                        entry.value = value
                elapsed = time.perf_counter() - started
                results["cache_assignment_seconds"] = round(elapsed, 4)
                results["cache_assignments_per_second"] = round(results["assignments"] / elapsed)
                started = time.perf_counter()
            elapsed = time.perf_counter() - started
            results["automatic_drain_seconds"] = round(elapsed, 4)
            results["persisted_documents_per_second"] = round(args.operations / elapsed)
            assert collection.count_documents({"value": args.mutations}) == args.operations
            assert collection.count_documents({"_orm_version": 2}) == args.operations
            results["acknowledged_document_writes"] = args.operations
        finally:
            assert database.startswith("pyorm_benchmark_")
            client.drop_database(database)
    output = json.dumps(results, ensure_ascii=False, indent=2)
    print(output)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
