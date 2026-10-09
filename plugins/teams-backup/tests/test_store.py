import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from teams_backup import store  # noqa: E402
from teams_backup.teams import deep_link, parse_chat_ref  # noqa: E402

THREAD = "19:meeting_ODJkZWI3ZTEtMDBhZC00MTc5LWI1NjctNzg4Y2QxMzkyOGIz@thread.v2"


def rec(i, ts, text="hi", author="A", **kw):
    r = {"id": str(i), "ts": ts, "type": "message", "author": author, "text": text, "attachments": [], "reactions": [], "edited": False}
    r.update(kw)
    return r


class ChatRef(unittest.TestCase):
    def test_forms(self):
        for ref in (THREAD,
                    "https://teams.microsoft.com/l/chat/%s/conversations?context=%%7B%%22contextType%%22%%3A%%22chat%%22%%7D" % THREAD,
                    "https://teams.cloud.microsoft/l/chat/%s/0" % THREAD.replace(":", "%3A").replace("@", "%40"),
                    "https://teams.cloud.microsoft/v2/#/conversations/%s?ctx=chat" % THREAD):
            self.assertEqual(parse_chat_ref(ref), THREAD)

    def test_bad(self):
        with self.assertRaises(Exception):
            parse_chat_ref("https://example.com")

    def test_deep_link(self):
        self.assertIn(THREAD, deep_link(THREAD))


class Jsonl(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "chat.jsonl")

    def test_append_is_incremental_and_deduplicated(self):
        a = rec(1, "2026-10-09T10:00:00.000Z")
        self.assertEqual(store.append_new(self.p, [a], "t"), (1, 0, 0))
        b = rec(2, "2026-10-09T11:00:00.000Z")
        self.assertEqual(store.append_new(self.p, [a, b], "t"), (1, 0, 1))
        self.assertEqual(len(store.read_jsonl(self.p)), 2)

    def test_edit_adds_revision_and_md_shows_latest_once(self):
        store.append_new(self.p, [rec(1, "2026-10-09T10:00:00.000Z", "old")], "t")
        self.assertEqual(store.append_new(self.p, [rec(1, "2026-10-09T10:00:00.000Z", "new", edited=True)], "t"), (0, 1, 0))
        md = store.render_md(store.read_jsonl(self.p), {"title": "T", "tz": "UTC"})
        self.assertIn("new", md)
        self.assertNotIn("old", md)
        self.assertEqual(md.count("**10:00 A"), 1)

    def test_torn_last_line_is_ignored(self):
        with open(self.p, "w") as f:
            f.write(json.dumps(rec(1, "2026-10-09T10:00:00.000Z")) + "\n{\"id\": \"2\", \"ts\"")
        self.assertEqual(len(store.read_jsonl(self.p)), 1)

    def test_md_is_chronological_regardless_of_append_order(self):
        store.append_new(self.p, [rec(2, "2026-10-09T11:00:00.000Z", "second")], "t")
        store.append_new(self.p, [rec(1, "2026-10-09T10:00:00.000Z", "first")], "t")
        md = store.render_md(store.read_jsonl(self.p), {"tz": "UTC"})
        self.assertLess(md.index("first"), md.index("second"))

    def test_md_uses_chat_time_zone_and_links_files(self):
        r = rec(1, "2026-10-09T12:47:00.000Z", attachments=[{"name": "a b.zip", "file": "attachments/a b.zip"}, {"name": "x", "error": "gone"}])
        md = store.render_md([r], {"tz": "Nowhere/Zone", "tz_offset": -180})
        self.assertIn("## 2026-10-09", md)
        self.assertIn("**09:47 A**", md)
        self.assertIn("(attachments/a%20b.zip)", md)
        self.assertIn("not downloaded", md)


class Times(unittest.TestCase):
    def test_window(self):
        tz = store.get_tz(None, -180)
        lo = store.parse_when("2026-10-09", tz)
        hi = store.parse_when("2026-10-09", tz, end=True)
        self.assertEqual(lo.isoformat(), "2026-10-09T03:00:00+00:00")
        self.assertEqual(hi - lo, dt.timedelta(days=1))
        self.assertIsNone(store.parse_when("", tz))

    def test_meeting_label(self):
        tz = store.get_tz(None, 120)
        s, e = store.parse_meeting_label("Friday, October 9, 2026 9:30 AM -  10:00 AM", tz)
        self.assertEqual((s.hour, s.minute, e.hour), (9, 30, 10))
        self.assertEqual(store.meeting_filename(s, 'Plan: "Q4" / go'), "2026-10-09_0930_Plan_ _Q4_ _ go.md")
        self.assertIsNone(store.parse_meeting_label("garbage", tz))

    def test_entry_labels(self):
        self.assertEqual(store.split_entry_label("Andrei Salanoi 0 minutes 3 seconds"), ("Andrei Salanoi", 3))
        self.assertEqual(store.split_entry_label("Ann Lee 1 hour 2 minutes 3 seconds"), ("Ann Lee", 3723))
        self.assertEqual(store.split_entry_label(" "), (None, None))
        self.assertEqual(store.fmt_offset(3723), "1:02:03")

    def test_transcript_render(self):
        entries = [{"label": " ", "text": "X started transcription"},
                   {"label": "A 0 minutes 3 seconds", "text": "one"}, {"label": "A 0 minutes 9 seconds", "text": "two"},
                   {"label": "B 0 minutes 12 seconds", "text": "three"}]
        md = store.render_transcript(entries, "Chat", "label", dt.datetime(2026, 10, 9, 9, 30), "UTC")
        self.assertEqual(md.count("**A**"), 1)
        self.assertIn("`0:09` two", md)
        self.assertIn("_X started transcription_", md)


if __name__ == "__main__":
    unittest.main()
