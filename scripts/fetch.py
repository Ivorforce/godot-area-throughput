"""Fetch godotengine/godot PRs into the year-sharded raw store.

  backfill     everything, newest-first, resumable via cursor checkpoint
  incremental  everything updated since the watermark (minus slack), upserted

Both store the same compact record shape; see README for the schema.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import gh_gql
import ndjson_store

OWNER = "godotengine"
NAME = "godot"
PAGE_SIZE = 25
WATERMARK_SLACK_DAYS = 3
MAX_INCREMENTAL_PAGES = 400

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WATERMARK = DATA_DIR / "watermark.json"
LABEL_REGISTRY = DATA_DIR / "labels.json"
BUFFER = DATA_DIR / "raw" / "backfill-buffer.ndjson"
CURSOR_FILE = DATA_DIR / "raw" / ".backfill-cursor"
START_FILE = DATA_DIR / "raw" / ".backfill-start"

PAGE_QUERY = """
query($cursor:String, $n:Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner:"%s", name:"%s") {
    pullRequests(first:$n, after:$cursor, orderBy:{field:%%s, direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title state isDraft createdAt closedAt mergedAt updatedAt
        baseRefName authorAssociation additions deletions
        author { login __typename }
        timelineItems(last:1, itemTypes:[CLOSED_EVENT]) {
          nodes { ... on ClosedEvent { actor { login __typename } } }
        }
        labels(first:25) { totalCount nodes { id } }
        reviews(first:50) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { author { login __typename } state submittedAt }
        }
      }
    }
  }
}
""" % (OWNER, NAME)

LABELS_QUERY = """
query($cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:"%s", name:"%s") {
    labels(first:100, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { id name color }
    }
  }
}
""" % (OWNER, NAME)

REMAINDER_QUERY = """
query($num:Int!, $cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:"%s", name:"%s") {
    pullRequest(number:$num) {
      reviews(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login __typename } state submittedAt }
      }
    }
  }
}
""" % (OWNER, NAME)


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_reviews_remainder(number, cursor):
    nodes = []
    while True:
        data = gh_gql.run(REMAINDER_QUERY, {"num": number, "cursor": cursor})
        conn = data["repository"]["pullRequest"]["reviews"]
        nodes += conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = conn["pageInfo"]["endCursor"]


def normalize(node):
    """GraphQL PR node -> compact stored record."""
    rconn = node["reviews"]
    raw_reviews = list(rconn["nodes"])
    truncated = False
    if rconn["pageInfo"]["hasNextPage"]:
        try:
            raw_reviews += fetch_reviews_remainder(
                node["number"], rconn["pageInfo"]["endCursor"])
        except gh_gql.GqlError as e:
            truncated = True
            print(f"WARN #{node['number']}: review remainder fetch failed: {e}",
                  file=sys.stderr)

    if node["labels"]["totalCount"] > 25:
        print(f"WARN #{node['number']}: more than 25 labels, some dropped",
              file=sys.stderr)

    reviews = []
    for r in raw_reviews:
        if not r or not r.get("submittedAt"):  # PENDING reviews are never submitted
            continue
        who = r.get("author") or {}
        reviews.append({"a": who.get("login"), "t": who.get("__typename"),
                        "s": r["state"], "at": r["submittedAt"]})
    reviews.sort(key=lambda r: r["at"])

    closed_events = node["timelineItems"]["nodes"]
    closer = ((closed_events[-1] or {}).get("actor") or {}) if closed_events else {}

    author = node.get("author") or {}
    record = {
        "number": node["number"],
        "closedBy": closer.get("login"),
        "closedByType": closer.get("__typename"),
        "title": node["title"],
        "state": node["state"],
        "isDraft": node["isDraft"],
        "createdAt": node["createdAt"],
        "closedAt": node["closedAt"],
        "mergedAt": node["mergedAt"],
        "baseRef": node["baseRefName"],
        "author": author.get("login"),
        "authorType": author.get("__typename"),
        "assoc": node["authorAssociation"],
        "additions": node["additions"],
        "deletions": node["deletions"],
        "labels": sorted(l["id"] for l in node["labels"]["nodes"]),
        "reviewsTotal": rconn["totalCount"],
        "reviewsTruncated": truncated,
        "reviews": reviews,
    }

    # Never downgrade: if the remainder fetch failed but the store already has
    # a complete version of this PR, keep the (stale but complete) old record —
    # the next update to the PR retries the fetch anyway.
    if truncated:
        old = ndjson_store.lookup(record["number"], record["createdAt"])
        if old and not old.get("reviewsTruncated"):
            print(f"WARN #{record['number']}: keeping previously stored complete "
                  f"record over truncated refetch", file=sys.stderr)
            return old
    return record


def write_watermark(iso_ts):
    WATERMARK.write_text(json.dumps({"lastRun": iso_ts}, indent=1) + "\n")


def update_label_registry():
    """Upsert id -> {name, color} for every existing label. Never prunes:
    a deleted label keeps its last-known entry so stored PRs still resolve."""
    registry = json.loads(LABEL_REGISTRY.read_text()) if LABEL_REGISTRY.exists() else {}
    registry = {k: v if isinstance(v, dict) else {"name": v, "color": None}
                for k, v in registry.items()}
    cursor = None
    while True:
        data = gh_gql.run(LABELS_QUERY, {"cursor": cursor})
        conn = data["repository"]["labels"]
        registry.update({l["id"]: {"name": l["name"], "color": l["color"]}
                         for l in conn["nodes"]})
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    LABEL_REGISTRY.write_text(json.dumps(registry, indent=1, sort_keys=True) + "\n")
    print(f"label registry: {len(registry)} entries", file=sys.stderr)


def backfill():
    ndjson_store.RAW_DIR.mkdir(parents=True, exist_ok=True)
    cursor = CURSOR_FILE.read_text().strip() if CURSOR_FILE.exists() else None
    if cursor:
        start = START_FILE.read_text().strip()
        seen = sum(1 for _ in BUFFER.open()) if BUFFER.exists() else 0
        mode = "a"
    else:
        start = utcnow_iso()
        START_FILE.write_text(start)
        seen = 0
        mode = "w"

    query = PAGE_QUERY % "CREATED_AT"
    with BUFFER.open(mode) as out:
        while True:
            data = gh_gql.run(query, {"cursor": cursor, "n": PAGE_SIZE})
            conn = data["repository"]["pullRequests"]
            for node in conn["nodes"]:
                out.write(ndjson_store.dumps(normalize(node)) + "\n")
                seen += 1
            out.flush()

            oldest = conn["nodes"][-1]["createdAt"][:10] if conn["nodes"] else "?"
            print(f"{seen:6d} PRs | oldest {oldest} | "
                  f"rate remaining {data['rateLimit']['remaining']}", file=sys.stderr)

            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
            CURSOR_FILE.write_text(cursor)

    # Finalize: buffer -> shards. Dedupe keeps the last occurrence (a resumed
    # run can refetch the page it was interrupted on).
    records = {}
    with BUFFER.open() as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                records[r["number"]] = r
    ndjson_store.upsert(records.values())
    update_label_registry()
    write_watermark(start)
    # Cursor first: a crash between unlinks must not leave a resume state that
    # points into a deleted buffer.
    CURSOR_FILE.unlink(missing_ok=True)
    START_FILE.unlink(missing_ok=True)
    BUFFER.unlink(missing_ok=True)
    print(f"done: {len(records)} PRs -> {ndjson_store.RAW_DIR}", file=sys.stderr)


def incremental():
    if not WATERMARK.exists():
        sys.exit("no data/watermark.json — run `fetch.py backfill` first")
    last_run = json.loads(WATERMARK.read_text())["lastRun"]
    cutoff_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
    cutoff = (cutoff_dt.timestamp() - WATERMARK_SLACK_DAYS * 86400)
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_start = utcnow_iso()

    query = PAGE_QUERY % "UPDATED_AT"
    records = []
    cursor = None
    pages = 0
    done = False
    while not done:
        pages += 1
        if pages > MAX_INCREMENTAL_PAGES:
            sys.exit(f"aborting: exceeded {MAX_INCREMENTAL_PAGES} pages since "
                     f"{cutoff_iso} — corrupted watermark?")
        data = gh_gql.run(query, {"cursor": cursor, "n": PAGE_SIZE})
        conn = data["repository"]["pullRequests"]
        for node in conn["nodes"]:
            if node["updatedAt"] < cutoff_iso:
                done = True
                break
            records.append(normalize(node))
        print(f"{len(records):5d} PRs updated since {cutoff_iso[:10]} | "
              f"rate remaining {data['rateLimit']['remaining']}", file=sys.stderr)
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]

    ndjson_store.upsert(records)
    update_label_registry()
    write_watermark(run_start)
    print(f"done: upserted {len(records)} PRs", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("backfill", "incremental"))
    args = ap.parse_args()
    backfill() if args.mode == "backfill" else incremental()


if __name__ == "__main__":
    main()
