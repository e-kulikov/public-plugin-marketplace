import unittest

import helpers  # noqa: F401  (sets sys.path)
from chatgpt_backup import plan as P

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


class ParseIds(unittest.TestCase):
    def test_space_comma_and_links(self):
        got = P.parse_ids([A + "," + B, "https://chatgpt.com/c/" + A, "  "])
        self.assertEqual(got, [A, B])

    def test_project_link_and_uppercase(self):
        self.assertEqual(P.parse_ids(["https://chatgpt.com/g/g-p-abc/c/" + A.upper()]), [A])

    def test_invalid(self):
        with self.assertRaises(P.ArgError):
            P.parse_ids(["not-an-id"])


class ParseTime(unittest.TestCase):
    def test_date_bounds(self):
        lo = P.parse_time("2026-01-01")
        hi = P.parse_time("2026-01-01", end=True)
        self.assertAlmostEqual(hi - lo, 86399.999, places=2)

    def test_iso_and_offset(self):
        self.assertEqual(P.parse_time("2026-01-01T00:00:00Z"), P.parse_time("2026-01-01T03:00:00+03:00"))
        self.assertEqual(P.parse_time("2026-01-01T00:00:00"), P.parse_time("2026-01-01T00:00:00Z"))

    def test_epoch_and_garbage(self):
        self.assertEqual(P.parse_time("1700000000"), 1700000000.0)
        with self.assertRaises(P.ArgError):
            P.parse_time("yesterday")


class Modes(unittest.TestCase):
    def test_exclusions(self):
        v = P.validate_mode
        self.assertEqual(v([A], None, None, False, False), "ids")
        self.assertEqual(v([], 1, 2, False, False), "range")
        self.assertEqual(v([], None, None, True, False), "all")
        self.assertEqual(v([], 1, None, False, True), "missing")      # --missing + --from is allowed
        self.assertIsNone(v([], None, None, False, False))
        for bad in (([A], 1, None, False, False), ([A], None, None, True, False), ([A], None, None, False, True),
                    ([], 1, None, True, False), ([], None, None, True, True), ([], 5, 1, False, False)):
            with self.assertRaises(P.ArgError):
                v(*bad)


def entry(cid, upd=1000.0, list_upd=None, archived=False, project_id=None, project=None, files=None, deleted=False):
    e = {"id": cid, "update_time": upd, "archived": archived, "project_id": project_id,
         "project": project, "files": files or [], "deleted_on_server": deleted}
    if list_upd is not None:
        e["list_update_time"] = list_upd
    return e


def it(cid, upd, archived=False, project_id=None):
    return {"id": cid, "update_time": upd, "archived": archived, "project_id": project_id}


class Diff(unittest.TestCase):
    def test_new_updated_unchanged(self):
        listing = [it("a", 2000), it("b", 1000), it("c", 5000)]
        man = [entry("a", list_upd=1000), entry("b", list_upd=1000)]
        plan = P.build_plan("missing", listing, man)
        self.assertEqual(plan["fetch"], {"a": "updated", "c": "new"})
        self.assertEqual(plan["unchanged"], 1)

    def test_legacy_manifest_tolerance(self):
        # schema-1 entry: only the conversation's own update_time; list time is never later when unchanged
        man = [entry("a", upd=1000.0), entry("b", upd=1000.0)]
        listing = [it("a", 999.0), it("b", 1003.0)]
        plan = P.build_plan("missing", listing, man)
        self.assertEqual(list(plan["fetch"]), ["b"])

    def test_range_filter_with_missing(self):
        listing = [it("a", 100), it("b", 900), it("c", 5000)]
        plan = P.build_plan("missing", listing, [], time_from=500, time_to=1000)
        self.assertEqual(list(plan["fetch"]), ["b"])

    def test_range_without_missing_refetches(self):
        man = [entry("a", list_upd=100)]
        plan = P.build_plan("range", [it("a", 100)], man, time_from=0, time_to=200)
        self.assertEqual(plan["fetch"], {"a": "in range"})

    def test_ids_mode_unknown_and_no_sync(self):
        man = [entry("zzz", list_upd=1)]
        plan = P.build_plan("ids", [it("a", 1)], man, ids=["a", "ghost"])
        self.assertEqual(set(plan["fetch"]), {"a", "ghost"})
        self.assertEqual(plan["unknown_ids"], ["ghost"])
        self.assertEqual(plan["deleted"], [])          # ids mode never flags others

    def test_sync_moves_archive_deleted_restored(self):
        man = [entry("m", list_upd=1, project_id=None, project=None),
               entry("r", list_upd=1, archived=True),
               entry("gone", list_upd=1),
               entry("back", list_upd=1, deleted=True)]
        listing = [it("m", 1, project_id="p1"), it("r", 1, archived=False), it("back", 1)]
        plan = P.build_plan("missing", listing, man, project_names={"p1": "Proj"})
        changes = {s["id"]: s["change"] for s in plan["sync"]}
        self.assertEqual(changes["m"], "moved")
        self.assertEqual(changes["r"], "unarchived")
        self.assertEqual(changes["back"], "restored")
        self.assertEqual(plan["deleted"], ["gone"])

    def test_renamed_project_counts_as_move(self):
        man = [entry("a", list_upd=1, project_id="p1", project="Old")]
        plan = P.build_plan("missing", [it("a", 1, project_id="p1")], man, project_names={"p1": "New"})
        self.assertEqual(plan["sync"], [{"id": "a", "change": "moved"}])

    def test_incomplete_files_retried(self):
        man = [entry("a", list_upd=1, files=[{"id": "f", "status": "error"}])]
        self.assertEqual(P.build_plan("missing", [it("a", 1)], man)["fetch"], {"a": "incomplete"})
        self.assertEqual(P.build_plan("missing", [it("a", 1)], man, want_files=False)["fetch"], {})

    def test_mass_deletion_is_suspected_not_applied(self):
        man = [entry("m%d" % i, list_upd=1) for i in range(30)]
        listing = [it("m%d" % i, 1) for i in range(10)]          # 20 of 30 "vanished"
        plan = P.build_plan("missing", listing, man)
        self.assertEqual(plan["deleted"], [])
        self.assertEqual(len(plan["deleted_suspicious"]), 20)
        self.assertIn("NOT flagged", P.format_report(P.summarize(plan), "missing", "/o"))
        plan = P.build_plan("missing", listing, man, allow_mass_delete=True)
        self.assertEqual(len(plan["deleted"]), 20)
        small = P.build_plan("missing", [it("m0", 1)], man[:3])   # 2 of 3 gone: below the 5-item floor
        self.assertEqual(len(small["deleted"]), 2)

    def test_report_mentions_estimate(self):
        plan = P.build_plan("all", [it("a", 1)], [])
        text = P.format_report(P.summarize(plan), "all", "/out")
        self.assertIn("~1 min", text)
        self.assertIn("left as they are", text)


if __name__ == "__main__":
    unittest.main()
