"""One-off helper: append a saved ClickHouse-MCP JSON result file into a target CSV (append mode).
Used when a query result is too large to return inline -- the MCP tool still saves the full JSON
result to a local file, and this script converts/appends that into the pipeline's raw CSVs.
See pipeline/README_clickhouse_queries.md for the queries this supports.
"""
import json
import csv
import sys
from pathlib import Path


def main():
    src_path, dest_path = sys.argv[1], sys.argv[2]
    with open(src_path) as f:
        obj = json.load(f)
    data = obj["result"]["data"]
    dest = Path(dest_path)
    file_exists = dest.exists()
    keys = list(data[0].keys()) if data else []
    with open(dest, "a", newline="") as out:
        w = csv.DictWriter(out, fieldnames=keys)
        if not file_exists:
            w.writeheader()
        w.writerows(data)
    print(f"appended {len(data)} rows from {src_path} -> {dest_path}")


if __name__ == "__main__":
    main()
