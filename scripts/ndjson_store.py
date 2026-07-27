"""Year-sharded ndjson store for PR records.

Shards live in data/raw/prs-<YYYY>.ndjson, keyed by year of createdAt
(immutable, so a PR never migrates between shards). Output is byte-stable:
records sorted by number, keys sorted, compact separators — so re-running
on identical data produces identical bytes and git diffs stay minimal.
"""

import json
from pathlib import Path

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def dumps(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def shard_path(year):
    return RAW_DIR / f"prs-{year}.ndjson"


def year_of(record):
    return int(record["createdAt"][:4])


def load_shard(year):
    """Return {number: record} for one shard; empty dict if absent."""
    path = shard_path(year)
    if not path.exists():
        return {}
    with path.open() as f:
        records = (json.loads(line) for line in f if line.strip())
        return {r["number"]: r for r in records}


def write_shard(year, records):
    """Write {number: record} sorted by number. Atomic: a crash mid-write must
    not leave a truncated shard that breaks every later run."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = shard_path(year)
    tmp = path.with_suffix(".ndjson.tmp")
    with tmp.open("w") as f:
        for number in sorted(records):
            f.write(dumps(records[number]) + "\n")
    tmp.replace(path)


def upsert(records):
    """Insert-or-replace records, grouped by shard year."""
    by_year = {}
    for r in records:
        by_year.setdefault(year_of(r), {})[r["number"]] = r
    for year, changed in sorted(by_year.items()):
        shard = load_shard(year)
        shard.update(changed)
        write_shard(year, shard)


def lookup(number, created_at):
    """Return the stored record for one PR, or None."""
    return load_shard(int(created_at[:4])).get(number)


def iter_all():
    """Yield every stored record, shard by shard."""
    for path in sorted(RAW_DIR.glob("prs-*.ndjson")):
        with path.open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
