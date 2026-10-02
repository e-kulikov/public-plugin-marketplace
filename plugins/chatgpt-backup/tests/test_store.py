import json
import os
import tempfile
import unittest

from helpers import write
from chatgpt_backup import store as S


class ManifestTests(unittest.TestCase):
    def test_legacy_list_is_loaded_and_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            folder = "no-project/2026-01-01_x__abc"
            os.makedirs(os.path.join(d, folder))
            write(os.path.join(d, folder, "conversation.json"), {"create_time": 10.0, "update_time": 20.5, "gizmo_id": "p1"})
            write(os.path.join(d, "manifest.json"), [{"id": "abc", "title": "x", "folder": folder, "files": []}])
            m = S.Manifest.load(d)
            self.assertEqual(m.schema, 1)
            self.assertEqual(S.migrate(d, m), 1)
            e = m.get("abc")
            self.assertEqual((e["create_time"], e["update_time"], e["project_id"]), (10.0, 20.5, "p1"))
            self.assertFalse(e["deleted_on_server"])
            m.save()
            self.assertEqual(S.Manifest.load(d).schema, 2)
            self.assertEqual(S.migrate(d, m), 0)     # idempotent

    def test_atomic_save_keeps_old_file_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            m = S.Manifest.load(d)
            m.upsert({"id": "a", "folder": "f"})
            m.save()
            m.upsert({"id": "b", "folder": object()})   # not serialisable
            with self.assertRaises(TypeError):
                m.save()
            self.assertEqual([c["id"] for c in S.Manifest.load(d).conversations], ["a"])
            self.assertEqual([f for f in os.listdir(d) if f.startswith(".tmp-")], [])

    def test_freshest(self):
        m = S.Manifest("x")
        m.upsert({"id": "a", "update_time": 5.0})
        m.upsert({"id": "b", "update_time": 9.0})
        self.assertEqual(m.freshest_update_time(), 9.0)


class Layout(unittest.TestCase):
    def test_folders(self):
        self.assertEqual(S.new_folder(1700000000.0, "Hi there!", "cid", None, None),
                         os.path.join("no-project", "2023-11-14_Hi_there__cid"))
        self.assertEqual(S.new_folder(1700000000.0, "T", "cid", "My Proj", "g-p-1"),
                         os.path.join("projects", "My_Proj", "2023-11-14_T__cid"))
        # title change keeps the leaf; project change moves it
        self.assertEqual(S.retarget("no-project/2023-11-14_Old__cid", "P", "g-p-1"),
                         os.path.join("projects", "P", "2023-11-14_Old__cid"))

    def test_move_and_prune(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "projects/P/leaf"))
            write(os.path.join(d, "projects/P/leaf/x"), "1")
            self.assertTrue(S.move_folder(d, "projects/P/leaf", "no-project/leaf"))
            self.assertTrue(os.path.exists(os.path.join(d, "no-project/leaf/x")))
            S.prune_empty_dirs(d, "projects/P")
            self.assertFalse(os.path.exists(os.path.join(d, "projects")))

    def test_move_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "a"))
            os.makedirs(os.path.join(d, "b"))
            with self.assertRaises(FileExistsError):
                S.move_folder(d, "a", "b")

    def test_index_and_unavailable(self):
        m = S.Manifest("x")
        m.upsert({"id": "a", "title": "A|B", "project": None, "created": "2026-01-01 00:00", "folder": "no-project/a b",
                  "messages": 2, "archived": True, "deleted_on_server": True,
                  "files": [{"id": "file_1", "kind": "image", "status": "unavailable", "reason": "gone"},
                            {"id": "file_2", "kind": "image", "status": "ok"}]})
        idx, un = S.render_index(m), S.render_unavailable(m)
        self.assertIn("archived, deleted on server", idx)
        self.assertIn("A\\|B", idx)
        self.assertIn("no-project/a%20b/conversation.md", idx)
        self.assertIn("(1 unavailable)", idx)
        self.assertIn("`file_1`", un)
        self.assertNotIn("file_2", un)


if __name__ == "__main__":
    unittest.main()
