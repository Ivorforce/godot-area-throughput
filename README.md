# godot-area-throughput

Monthly tracker of PR review throughput per label for
[godotengine/godot](https://github.com/godotengine/godot), published as a static
GitHub Pages site. Answers: where does review lag behind incoming PRs, how much
review is happening at all, and who reviews / authors what, per area.

Built as decision support for Godot maintainers — spotting under-served areas,
capacity gaps, and fragility (areas depending on a single reviewer). Everything
here is aggregated from public GitHub data. It measures **area throughput, not
individual performance**: the numbers count contributions made, never
obligations, and cannot see review depth, mentoring, design discussion, or any
work that happens outside PR reviews. Most contributors are volunteers.

This tool was written largely with AI assistance (Claude), directed and
reviewed by a Godot maintainer. It contains no engine code.

## Metric definitions

- **Qualifying review**: submitted by someone other than the PR author who is
  not a bot (`Bot` account type, `[bot]` login suffix, or
  `copilot-pull-request-reviewer`). Any state, including comment-only and
  dismissed reviews. This excludes authors' replies to review threads, which
  GitHub records as reviews.
- **reviewed**: distinct PRs receiving ≥1 qualifying review that month —
  a PR reviewed in several months counts in each (it measures review activity).
  Note this deliberately measures activity, not depth: a formatting pass and a
  deep technical review count the same, because no API signal separates them.
- **decided**: distinct PRs that month with an approve or changes-requested
  qualifying review, or closed unmerged by a non-author non-bot (the stored
  `closedBy` actor). Includes the release team's routine closes of
  superseded/stale PRs — those are not distinguishable from technical
  rejections without encoding role knowledge.
- **opened / merged / closed unmerged**: by createdAt / mergedAt / closedAt.
  The page's Flow chart shows opened vs closed (merged + unmerged combined).
- **openAtEnd**: open PRs carrying the label at month end — emitted in the data
  files for other consumers, not currently charted.
- **resolution rate**: of the PRs opened in a month, the share closed (merged
  or not) within 7/60/365 days. A month that hasn't yet had the full 7/60/365
  days is left out entirely rather than shown misleadingly fast. The data
  files carry raw closure counts plus a last-judgeable-month index; the page
  computes the rates, and its 3-month average combines the three months' PRs
  into one pool and computes the rate over the pool, so small months don't
  distort it.
- The **all** pseudo-label covers every PR, including unlabeled ones.
- The current partial month is excluded from all series. Labels applied to
  fewer than 20 PRs all-time are dropped.

Overview table (from `index.json`, trailing 12 complete months):

- **net 12m**: opened minus closed, shown against opened.
- **resolved ≤60d**: 60-day resolution rate over the 12 most recent months old
  enough to judge, pulled toward the all-PRs average (by mixing in 20 phantom
  average-behaving PRs) so labels with very few PRs can't land at the top or
  bottom of the ranking by luck.
- **top decider**: of the label's PRs that got an approve or changes-requested
  decision, the share decided by the single most active person; "—" below 10
  such PRs. Closes are deliberately not counted here (unlike the monthly
  *decided* series) because the release team closes most PRs as routine
  cleanup, which would otherwise look like area ownership.
- Labels applied when a PR is closed or merged (`salvageable`, `archived`,
  `spam`, `cherrypick:*`) are excluded from the ranking — their stats describe
  outcomes, not how the area is served. They remain in the selector with full
  detail data.

## How it works

- `data/raw/prs-<YYYY>.ndjson` — one compact record per PR (sharded by creation
  year, sorted by number, byte-stable). Labels are stored as **IDs**; a rename
  retroactively unifies history, and `data/labels.json` (id → {name, color},
  upserted every run, never pruned) resolves them at aggregation time. Records
  also store `closedBy`/`closedByType` (the actor of the last close event) so
  counting a close as a decision (see *decided* above) doesn't require
  re-fetching every PR. Record shape:

  ```json
  {"number":121137,"title":"…","state":"OPEN","isDraft":false,
   "createdAt":"…","closedAt":null,"mergedAt":null,
   "closedBy":null,"closedByType":null,"baseRef":"master",
   "author":"login","authorType":"User","assoc":"CONTRIBUTOR",
   "additions":3,"deletions":0,"labels":["<label-node-id>", "…"],
   "reviewsTotal":2,"reviewsTruncated":false,
   "reviews":[{"a":"login","t":"User","s":"APPROVED","at":"…"}]}
  ```
- `scripts/fetch.py backfill` — fetch every PR via GraphQL, resumable through a
  cursor checkpoint. Run once, locally.
- `scripts/fetch.py incremental` — fetch everything updated since the watermark
  (`data/watermark.json`, the previous successful run's start time) minus 3
  days of slack, and upsert. Run monthly by CI. Reviews landing on old PRs are
  picked up because they bump `updatedAt`.
- `scripts/aggregate.py` — recompute all per-label monthly series from the raw
  store into `docs/data/` (index + one file per label). All metric definitions
  live here; new metrics recompute over history without refetching.
- `docs/` — the Pages site (branch-based: main + `/docs`).

## Caveats

- Labels are read as they are today: renamed labels keep their full history
  (they're tracked by ID), deleted labels keep their last-known name, and each
  label's charts start at its first-ever PR (early history undercounts).
- Resolution-rate lines stop before the present — months that haven't yet had
  the full 7/60/365 days aren't shown; deliberate, not missing data.
- Reviews only exist as a GitHub feature since late 2016; earlier history has
  opened/merged data only.
- Minor known limits: the "(N others)" table rows sum per-person credits, so
  their percentages can overlap; all deleted accounts merge into one
  "(deleted)" entry; on PRs whose author deleted their account, the author's
  own thread replies count as reviews (undetectable without the login).

## Runbook

No dependencies beyond Python 3 stdlib, the `gh` CLI, and a vendored
[Chart.js](https://github.com/chartjs/Chart.js) 4.4.9
(`docs/vendor/chart.umd.min.js`, from
`https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.min.js`, MIT).
Licensed [MIT](LICENSE).

One-time backfill (~2,000 pages, roughly an hour; interrupt and re-run to
resume):

```sh
python3 scripts/fetch.py backfill
python3 scripts/aggregate.py
python3 -m http.server -d docs 8000     # http://localhost:8000
```

Validation: compare `wc -l data/raw/*.ndjson` totals against the repository's
`pullRequests.totalCount`, and spot-check month buckets against GitHub search
(`repo:godotengine/godot is:pr label:topic:core created:2026-05-01..2026-05-31`).
Window-based check against a manual measurement:

```sh
python3 scripts/aggregate.py --spotcheck topic:core 2026-01-27 2026-07-27
```

Tests: `python3 -m unittest discover tests`

CI: `.github/workflows/update.yml` runs on the 2nd of each month (and via
manual dispatch), commits updated data, and requests a Pages build. Configure
Pages once in repo settings: Deploy from a branch → `main` → `/docs`. If runs
fail twice in a row, fix and dispatch manually — the watermark makes catch-up
automatic.
