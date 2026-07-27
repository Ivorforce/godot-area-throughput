import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import aggregate
import ndjson_store

FIXTURE = Path(__file__).resolve().parent / "fixture.ndjson"


def load_fixture():
    with FIXTURE.open() as f:
        return [json.loads(line) for line in f if line.strip()]


class TestAggregate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = aggregate.build_aggregates(
            load_fixture(), "2020-06", min_label_prs=0)
        cls.core = cls.result["labels"]["topic-core"]
        cls.all = cls.result["labels"]["all"]

    def test_months_axis(self):
        self.assertEqual(self.result["index"]["months"],
                         ["2020-01", "2020-02", "2020-03", "2020-04", "2020-05", "2020-06"])

    def test_flow_series(self):
        self.assertEqual(self.core["opened"], [3, 2, 2, 1, 0, 0])
        self.assertEqual(self.core["reviewed"], [0, 1, 2, 1, 1, 0])
        self.assertEqual(self.core["approved"], [0, 0, 1, 0, 0, 0])
        # Mar: PR4 approved. Apr: PR6 closed unmerged by non-author + PR9 CR.
        self.assertEqual(self.core["decided"], [0, 0, 1, 2, 0, 0])
        self.assertEqual(self.core["merged"], [0, 0, 1, 0, 0, 0])
        self.assertEqual(self.core["closedUnmerged"], [0, 0, 0, 1, 0, 0])
        self.assertEqual(self.core["reviewsSubmitted"], [0, 1, 2, 1, 1, 0])
        self.assertEqual(self.core["distinctReviewers"], [0, 1, 2, 1, 1, 0])

    def test_backlog(self):
        self.assertEqual(self.core["openAtEnd"], [3, 5, 6, 6, 6, 6])

    def test_resolution(self):
        res = self.core["resolution"]
        # Feb cohort: PR4 closed after 33d, PR6 after 55d — inside 60d, past 7d.
        self.assertEqual(res["d60"]["closed"], [0, 2, 0, 0, 0, 0])
        self.assertEqual(res["d7"]["closed"], [0, 0, 0, 0, 0, 0])
        self.assertEqual(res["d60"]["through"], 5)   # 2020 is long past
        gui = self.result["labels"]["topic-gui"]["resolution"]
        self.assertEqual(gui["d60"]["closed"], [0, 1, 0, 0, 1, 0])  # PR5, PR10

    def test_resolution_eligibility(self):
        result = aggregate.build_aggregates(
            load_fixture(), "2020-06", min_label_prs=0,
            now=datetime(2020, 7, 15, tzinfo=timezone.utc))
        res = result["labels"]["topic-core"]["resolution"]
        self.assertEqual(res["d7"]["through"], 5)    # even June had 7+ days
        self.assertEqual(res["d60"]["through"], 3)   # April is the last 60d+ cohort
        self.assertEqual(res["d365"]["through"], -1)

    def test_bot_and_self_reviews_do_not_qualify(self):
        # PRs 2 (self) and 3 (bots incl. copilot and Bot-typename) never reviewed.
        self.assertEqual(self.core["reviewed"][0], 0)

    def test_reviewer_table(self):
        rows = {r["login"]: r for r in self.core["reviewers12m"]}
        self.assertEqual(rows["carol"]["prs"], 2)
        self.assertEqual(rows["carol"]["decided"], 1)   # approved PR4
        self.assertEqual(rows["erin"]["decided"], 1)    # changes-requested PR9
        self.assertEqual(rows["(deleted)"]["decided"], 0)
        self.assertEqual(rows["carol"]["pctOfReviewed"], 50.0)  # of {4,6,7,9}
        self.assertEqual(rows["carol"]["pctOfDecided"], 50.0)   # of {4,9}
        self.assertEqual(rows["(deleted)"]["prs"], 1)           # ghost reviewer
        self.assertEqual(rows["erin"]["prs"], 1)

    def test_author_table(self):
        core = {r["login"]: r for r in self.core["authors12m"]}
        self.assertEqual(core["alice"],
                         {"login": "alice", "open": 2, "opened": 2, "pctOpen": 100.0})
        self.assertEqual(core["bob"],
                         {"login": "bob", "open": 1, "opened": 2, "pctOpen": 50.0})
        self.assertNotIn("dave", core)          # nothing still open -> no row
        self.assertEqual(core["(deleted)"]["open"], 1)          # ghost author
        gui = {r["login"]: r for r in self.result["labels"]["topic-gui"]["authors12m"]}
        self.assertNotIn("carol", gui)          # merged
        self.assertNotIn("alice", gui)          # withdrawn
        self.assertEqual(gui["gina"]["open"], 1)

    def test_all_pseudo_label(self):
        self.assertEqual(self.all["opened"], [3, 3, 2, 1, 1, 0])
        self.assertEqual(self.all["firstSeen"], "2020-01")

    def test_author_close_is_not_a_decision(self):
        gui = self.result["labels"]["topic-gui"]
        self.assertEqual(gui["closedUnmerged"], [0, 0, 0, 0, 1, 0])  # PR10 counted
        self.assertEqual(gui["decided"], [0, 0, 0, 1, 0, 0])         # but not decided

    def test_index_summary(self):
        entry = next(l for l in self.result["index"]["labels"]
                     if l["name"] == "topic:core")
        self.assertEqual(entry["opened12"], 8)
        self.assertEqual(entry["closed12"], 2)   # PR4 merged + PR6 closed
        # Raw: 2 of 8 within 60d; shrunk toward the global rate p0 = 4/10
        # with 20 pseudo-PRs: (2 + 20*0.4) / (8 + 20) = 35.7%.
        self.assertEqual(entry["res60"], 35.7)
        self.assertIsNone(entry["topDecider"])  # only 2 decisions in window
        self.assertEqual(entry["topDeciderN"], 2)

    def test_top_decider(self):
        original = aggregate.MIN_DECIDED_FOR_CONC
        aggregate.MIN_DECIDED_FOR_CONC = 0
        try:
            result = aggregate.build_aggregates(load_fixture(), "2020-06",
                                                min_label_prs=0)
        finally:
            aggregate.MIN_DECIDED_FOR_CONC = original
        entry = next(l for l in result["index"]["labels"]
                     if l["name"] == "topic:core")
        # carol and erin decided one of the two decided PRs each.
        self.assertEqual(entry["topDecider"], 50.0)

    def test_partial_month_pr_off_axis_but_in_totals(self):
        entry = next(l for l in self.result["index"]["labels"]
                     if l["name"] == "topic:core")
        self.assertEqual(entry["total"], 9)     # includes PR 12
        self.assertEqual(entry["openNow"], 7)
        self.assertEqual(sum(self.core["opened"]), 8)  # excludes PR 12

    def test_min_label_prs_filter(self):
        result = aggregate.build_aggregates(load_fixture(), "2020-06", min_label_prs=5)
        names = {l["name"] for l in result["index"]["labels"]}
        self.assertIn("topic:core", names)
        self.assertIn("all", names)      # pseudo-label survives any floor
        self.assertNotIn("topic:gui", names)
        self.assertNotIn("bug", names)

    def test_outcome_label_flag(self):
        self.assertTrue(aggregate.is_outcome_label("salvageable"))
        self.assertTrue(aggregate.is_outcome_label("cherrypick:4.6"))
        self.assertFalse(aggregate.is_outcome_label("topic:core"))
        self.assertTrue(aggregate.is_status_label("needs testing"))
        self.assertFalse(aggregate.is_status_label("topic:core"))
        flags = {l["name"]: l["outcome"] for l in self.result["index"]["labels"]}
        self.assertFalse(any(flags.values()))  # fixture has no outcome labels

    def test_index_grouping_and_order(self):
        groups = [l["group"] for l in self.result["index"]["labels"]]
        self.assertEqual(groups, sorted(groups, key={"special": 0, "topic": 1, "other": 2}.get))
        self.assertEqual(self.result["index"]["labels"][0]["name"], "all")


class TestStore(unittest.TestCase):
    def test_round_trip_byte_stability(self):
        records = load_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            original, ndjson_store.RAW_DIR = ndjson_store.RAW_DIR, Path(tmp)
            try:
                ndjson_store.upsert(records)
                first = {p.name: p.read_bytes() for p in Path(tmp).glob("*.ndjson")}
                ndjson_store.upsert(records)
                second = {p.name: p.read_bytes() for p in Path(tmp).glob("*.ndjson")}
                self.assertEqual(first, second)
                self.assertEqual(len(list(ndjson_store.iter_all())), len(records))
            finally:
                ndjson_store.RAW_DIR = original


if __name__ == "__main__":
    unittest.main()
