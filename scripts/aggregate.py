"""Aggregate the raw PR store into per-label monthly series for the site.

Reads data/raw/, writes docs/data/ (index.json + labels/<slug>.json).
All metric definitions live here; the page only renders.

  --spotcheck LABEL START END   print opened/reviewed for an arbitrary date
                                window (validation against manual counts)
"""

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ndjson_store

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "data"
REPO = "godotengine/godot"

ALL = "all"                  # pseudo-label: every PR, labeled or not
MIN_LABEL_PRS = 20           # drop one-off/typo labels; 0 disables
TABLE_MONTHS = 12            # trailing window for reviewer/author tables
TABLE_ROWS = 30
HORIZONS = (7, 60, 365)      # resolution-rate horizons, in days (~logarithmic)
RES60_PRIOR = 20             # pseudo-PRs pulling small labels' res60 toward the mean
MIN_DECIDED_FOR_CONC = 25    # decisions below which concentration shows a count, not a %

# Process/status labels mark a PR's state, not an area of the codebase. Their
# resolution stats restate the label's meaning ("needs work" PRs stall — that
# is why the label is there), so the overview lists them separately, without
# service columns.
STATUS_LABELS = {"needs testing", "needs work", "discussion", "feature proposal",
                 "for pr meeting"}


def is_status_label(name):
    return name in STATUS_LABELS

# Labels applied as part of an outcome (at close/merge), not at triage. Their
# cohort stats are conditioned on the outcome having happened, so they are
# kept out of the overview ranking; detail data stays available.
OUTCOME_LABELS = {"salvageable", "archived", "spam"}
OUTCOME_PREFIXES = ("cherrypick:",)


def is_outcome_label(name):
    return name in OUTCOME_LABELS or name.startswith(OUTCOME_PREFIXES)
BOT_LOGINS = {"copilot-pull-request-reviewer"}
DELETED = "(deleted)"


def is_bot(login, typename):
    return typename == "Bot" or (login or "").endswith("[bot]") or login in BOT_LOGINS


def qualifying_reviews(rec):
    """Reviews by someone other than the PR author who is not a bot."""
    author = rec["author"]
    return [r for r in rec["reviews"]
            if not (r["a"] is not None and r["a"] == author)
            and not is_bot(r["a"], r["t"])]


def month_range(first, last):
    """["2014-01", ..., last] inclusive."""
    months = []
    y, m = int(first[:4]), int(first[5:7])
    while True:
        months.append(f"{y:04d}-{m:02d}")
        if months[-1] == last:
            return months
        m += 1
        if m > 12:
            y, m = y + 1, 1


def month_end(m):
    """First instant after month m ("YYYY-MM")."""
    y, mo = int(m[:4]), int(m[5:7])
    y, mo = (y, mo + 1) if mo < 12 else (y + 1, 1)
    return datetime(y, mo, 1, tzinfo=timezone.utc)


def days_between(a, b):
    ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
    tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return (tb - ta).total_seconds() / 86400


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "label"


def build_aggregates(records, last_month, min_label_prs=MIN_LABEL_PRS, now=None):
    """Pure core: list of records + last complete month -> {"index":..., "labels": {slug: data}}."""
    now = now or datetime.now(timezone.utc)
    records = list(records)
    first_month = min(r["createdAt"][:7] for r in records)
    if first_month > last_month:
        raise ValueError("no records at or before last_month")
    months = month_range(first_month, last_month)
    idx = {m: i for i, m in enumerate(months)}
    n = len(months)
    window_start = max(0, n - TABLE_MONTHS)

    def blank():
        return {
            "opened": [0] * n, "reviewed": [0] * n, "approved": [0] * n,
            "decided": [0] * n,
            "merged": [0] * n, "closedUnmerged": [0] * n,
            "reviewsSubmitted": [0] * n, "openDelta": [0] * n,
            "reviewerSets": defaultdict(set),      # month i -> reviewer keys
            "closedWithin": {h: [0] * n for h in HORIZONS},  # by opening month
            "firstSeen": None, "total": 0, "openNow": 0, "truncated": 0,
            "rev12": defaultdict(set),             # reviewer -> pr numbers in window
            "dec12": defaultdict(set),             # reviewer -> pr numbers with verdict
            "reviewed12": set(), "decided12": set(),
            "auth12": defaultdict(lambda: [0, 0]),  # author -> [opened, stillOpen];
                                                    #   cohort: PRs opened in window
        }

    labels = defaultdict(blank)

    for rec in records:
        qrs = qualifying_reviews(rec)
        created_i = idx.get(rec["createdAt"][:7])
        merged_i = idx.get(rec["mergedAt"][:7]) if rec["mergedAt"] else None
        closed_i = idx.get(rec["closedAt"][:7]) if rec["closedAt"] else None
        author_key = rec["author"] or DELETED

        rev_months, app_months, dec_months = set(), set(), set()
        sub_counts = defaultdict(int)
        for r in qrs:
            i = idx.get(r["at"][:7])
            if i is None:
                continue
            rev_months.add(i)
            if r["s"] == "APPROVED":
                app_months.add(i)
            if r["s"] in ("APPROVED", "CHANGES_REQUESTED"):
                dec_months.add(i)
            sub_counts[i] += 1

        # A non-author, non-bot closing a PR unmerged is a decision too, and is
        # credited to the closer. Merges are not separately credited: in this
        # workflow the release team merges what reviewers approved — the
        # approval is the decision.
        closer = rec.get("closedBy")
        close_decider = None
        if (rec["state"] == "CLOSED" and closed_i is not None and closer
                and closer != rec["author"]
                and not is_bot(closer, rec.get("closedByType"))):
            dec_months.add(closed_i)
            close_decider = closer

        for name in set(rec["labels"]) | {ALL}:
            L = labels[name]
            L["total"] += 1
            if rec["state"] == "OPEN":
                L["openNow"] += 1
            if rec["reviewsTruncated"]:
                L["truncated"] += 1
            if created_i is None:
                continue  # created in the partial current month; off-axis
            cm = months[created_i]
            if L["firstSeen"] is None or cm < L["firstSeen"]:
                L["firstSeen"] = cm
            L["opened"][created_i] += 1
            L["openDelta"][created_i] += 1
            if closed_i is not None:
                L["openDelta"][closed_i] -= 1
            if merged_i is not None:
                L["merged"][merged_i] += 1
            if rec["state"] == "CLOSED" and closed_i is not None:
                L["closedUnmerged"][closed_i] += 1
            for i in rev_months:
                L["reviewed"][i] += 1
            for i in app_months:
                L["approved"][i] += 1
            for i in dec_months:
                L["decided"][i] += 1
            for i, c in sub_counts.items():
                L["reviewsSubmitted"][i] += c
            if rec["closedAt"]:
                delta = days_between(rec["createdAt"], rec["closedAt"])
                for h in HORIZONS:
                    if delta <= h:
                        L["closedWithin"][h][created_i] += 1
            for r in qrs:
                i = idx.get(r["at"][:7])
                if i is None:
                    continue
                key = r["a"] or DELETED
                L["reviewerSets"][i].add(key)
                if i >= window_start:
                    L["rev12"][key].add(rec["number"])
                    L["reviewed12"].add(rec["number"])
                    if r["s"] in ("APPROVED", "CHANGES_REQUESTED"):
                        L["dec12"][key].add(rec["number"])
                        L["decided12"].add(rec["number"])
            if close_decider and closed_i >= window_start:
                L["dec12"][close_decider].add(rec["number"])
                L["decided12"].add(rec["number"])
            if created_i >= window_start:
                a = L["auth12"][author_key]
                a[0] += 1
                if rec["state"] == "OPEN":
                    a[1] += 1

    # Drop rare labels (typos, one-offs); the pseudo-label always stays.
    kept = {name: L for name, L in labels.items()
            if name == ALL or L["total"] >= min_label_prs}
    if any(L["truncated"] for L in kept.values()):
        print(f"WARN: {labels[ALL]['truncated']} records have truncated reviews",
              file=sys.stderr)

    # Slugs: pseudo-label first, then sorted names; deterministic collision suffix.
    slugs = {}
    taken = set()
    for name in [ALL] + sorted(k for k in kept if k != ALL):
        slug = slugify(name)
        candidate, i = slug, 1
        while candidate in taken:
            i += 1
            candidate = f"{slug}-{i}"
        taken.add(candidate)
        slugs[name] = candidate

    def pct(x, total):
        return round(100 * x / total, 1) if total else 0.0

    # Months old enough that every PR opened in them has had 60 days to resolve.
    elig60 = [i for i in range(n)
              if month_end(months[i]) + timedelta(days=60) <= now][-TABLE_MONTHS:]

    # Global 60-day rate: the shrinkage prior for per-label res60, so labels
    # with a handful of PRs don't top or bottom the overview table.
    g_opened = sum(labels[ALL]["opened"][i] for i in elig60)
    p0 = (sum(labels[ALL]["closedWithin"][60][i] for i in elig60) / g_opened
          if g_opened else 0.0)

    label_files = {}
    summaries = {}
    for name, L in kept.items():
        open_at_end, running = [], 0
        for d in L["openDelta"]:
            running += d
            open_at_end.append(running)

        # Per-horizon closure counts plus the last judgeable month index
        # (eligibility is a prefix of the axis). The page computes the rates,
        # so its rolling average can pool cohorts weighted instead of
        # averaging percentages.
        resolution = {}
        for h in HORIZONS:
            eligible = [i for i in range(n)
                        if month_end(months[i]) + timedelta(days=h) <= now]
            resolution[f"d{h}"] = {
                "closed": L["closedWithin"][h],
                "through": eligible[-1] if eligible else -1,
            }

        def table(rows, remainder_label):
            head, rest = rows[:TABLE_ROWS], rows[TABLE_ROWS:]
            if rest:
                summed = [sum(r[k] for r in rest) for k in range(1, len(rows[0]))]
                head.append([f"({len(rest)} others)"] + summed)
            return head

        # Union of reviewers and closers: someone can decide (by closing)
        # without ever having left a review.
        people = set(L["rev12"]) | set(L["dec12"])
        rev_rows = sorted(([k, len(L["rev12"].get(k, ())), len(L["dec12"].get(k, ()))]
                           for k in people),
                          key=lambda r: (-r[2], -r[1], r[0]))
        reviewed12, decided12 = len(L["reviewed12"]), len(L["decided12"])
        reviewers12m = [
            {"login": k, "prs": c, "pctOfReviewed": pct(c, reviewed12),
             "decided": d, "pctOfDecided": pct(d, decided12)}
            for k, c, d in table(rev_rows, "others")]

        # Share of the area's decided PRs carried by its most active decider.
        # Fragility, not service quality: high = the area hangs on one person.
        # Below the threshold the page shows the raw count instead of a share —
        # a near-undecided area is the most starved state, not "no problem".
        decided_total = len(L["decided12"])
        top_decider = (round(100 * max(len(s) for s in L["dec12"].values())
                             / decided_total, 1)
                       if decided_total >= MIN_DECIDED_FOR_CONC else None)

        elig_opened = sum(L["opened"][i] for i in elig60)
        summaries[name] = {
            "topDecider": top_decider,
            "topDeciderN": decided_total,
            "opened12": sum(L["opened"][window_start:]),
            "closed12": sum(L["merged"][i] + L["closedUnmerged"][i]
                            for i in range(window_start, n)),
            "res60": (round(100 * (sum(L["closedWithin"][60][i] for i in elig60)
                                   + RES60_PRIOR * p0)
                            / (elig_opened + RES60_PRIOR), 1)
                      if elig_opened else None),
        }

        auth_rows = sorted(([k, s, o] for k, (o, s) in L["auth12"].items() if s),
                           key=lambda r: (-r[1], r[0]))
        authors12m = [
            {"login": k, "open": s, "opened": o, "pctOpen": pct(s, o)}
            for k, s, o in table(auth_rows, "others")]

        label_files[slugs[name]] = {
            "label": name,
            "firstSeen": L["firstSeen"],
            "opened": L["opened"],
            "reviewed": L["reviewed"],
            "approved": L["approved"],
            "decided": L["decided"],
            "merged": L["merged"],
            "closedUnmerged": L["closedUnmerged"],
            "reviewsSubmitted": L["reviewsSubmitted"],
            "distinctReviewers": [len(L["reviewerSets"].get(i, ())) for i in range(n)],
            "openAtEnd": open_at_end,
            "resolution": resolution,
            "reviewers12m": reviewers12m,
            "authors12m": authors12m,
        }

    def group(name):
        if name == ALL:
            return "special"
        return "topic" if name.startswith("topic:") else "other"

    order = {"special": 0, "topic": 1, "other": 2}
    index_labels = sorted(
        ({"name": name, "file": f"labels/{slugs[name]}.json", "group": group(name),
          "firstSeen": L["firstSeen"], "openNow": L["openNow"], "total": L["total"],
          "outcome": is_outcome_label(name), "status": is_status_label(name),
          **summaries[name]}
         for name, L in kept.items() if L["firstSeen"] is not None),
        key=lambda e: (order[e["group"]], e["name"]))

    index = {"repo": REPO, "months": months, "labels": index_labels}
    return {"index": index, "labels": label_files}


def last_complete_month(now=None):
    now = now or datetime.now(timezone.utc)
    y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def spotcheck(records, label, start, end):
    opened = reviewed = 0
    for rec in records:
        if label != ALL and label not in rec["labels"]:
            continue
        if start <= rec["createdAt"][:10] <= end:
            opened += 1
        if any(start <= r["at"][:10] <= end for r in qualifying_reviews(rec)):
            reviewed += 1
    print(f"{label} {start}..{end}: opened={opened} reviewed={reviewed}")


def write_output(result):
    tmp = OUT_DIR.parent / "data.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "labels").mkdir(parents=True)

    index = dict(result["index"])
    index["generatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp / "index.json").write_text(
        json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n")
    for slug, data in sorted(result["labels"].items()):
        (tmp / "labels" / f"{slug}.json").write_text(
            json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    tmp.rename(OUT_DIR)
    print(f"wrote {len(result['labels'])} labels x {len(index['months'])} months "
          f"-> {OUT_DIR}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spotcheck", nargs=3, metavar=("LABEL", "START", "END"))
    args = ap.parse_args()

    records = list(ndjson_store.iter_all())
    if not records:
        sys.exit("raw store is empty — run fetch.py backfill first")

    # Records store label IDs; resolve to current names via the registry so a
    # renamed label retroactively unifies its whole history. Unknown IDs
    # (never expected) pass through verbatim.
    registry_path = ROOT / "data" / "labels.json"
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    registry = {k: v if isinstance(v, dict) else {"name": v, "color": None}
                for k, v in registry.items()}
    if any(v["name"] == ALL for v in registry.values()):
        print(f"WARN: a real label named {ALL!r} exists and will merge into "
              f"the pseudo-label", file=sys.stderr)
    for rec in records:
        rec["labels"] = [registry[i]["name"] if i in registry else i
                         for i in rec["labels"]]

    if args.spotcheck:
        spotcheck(records, *args.spotcheck)
        return
    result = build_aggregates(records, last_complete_month())
    colors = {v["name"]: v["color"] for v in registry.values()}
    for entry in result["index"]["labels"]:
        entry["color"] = colors.get(entry["name"])
    write_output(result)


if __name__ == "__main__":
    main()
