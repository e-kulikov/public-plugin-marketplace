import json
import os
import tempfile
import unittest

from helpers import FakeApi, item, make_conv, msg, read
from chatgpt_backup import store
from chatgpt_backup.run import Lock, LockError, Runner

CID1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CID2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
IMG = {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_ok1"}
IMG2 = {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_gone"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def silent(_m):
    pass


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "backup")
        self.dl = os.path.join(self.tmp.name, "dl")
        self.api = FakeApi()
        self.api.links["file_ok1"] = ("ok", "u1")
        self.api.blobs["u1"] = PNG
        self.projects = {"g-p-1": {"id": "g-p-1", "name": "Cooking", "raw": {
            "gizmo": {"instructions": "be tasty", "display": {"description": "d"}},
            "files": [{"name": "r.md", "file_id": "file_x", "type": "text/markdown", "size": 3}]}}}

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, **kw):
        return Runner(self.out, self.api, self.projects, downloads_dir=self.dl, log=silent, sleep=lambda s: None, **kw)

    def conv(self, cid, **kw):
        c = make_conv(cid, messages=[msg("user", ["pic", IMG, IMG2], ctype="multimodal_text"), msg("assistant", ["nice"])], **kw)
        self.api.convs[cid] = c
        return c

    def execute(self, r, fetch, listing, sync=(), deleted=()):
        plan = {"fetch": fetch, "sync": list(sync), "deleted": list(deleted)}
        return r.execute(plan, {i["id"]: i for i in listing})

    def test_new_conversation_with_files(self):
        self.conv(CID1)
        r = self.runner()
        fails = self.execute(r, {CID1: "new"}, [item(CID1)])
        self.assertEqual(fails, [])
        e = store.Manifest.load(self.out).get(CID1)
        folder = os.path.join(self.out, e["folder"])
        self.assertTrue(os.path.exists(os.path.join(folder, "conversation.json")))
        md = read(os.path.join(folder, "conversation.md"))
        self.assertIn("![image](artifacts/file_ok1__.png)", md)
        self.assertIn("image unavailable: file not found on server (404)", md)
        st = {f["id"]: f["status"] for f in e["files"]}
        self.assertEqual(st, {"file_ok1": "ok", "file_gone": "unavailable"})
        self.assertEqual(e["messages"], 2)
        self.assertEqual(e["list_update_time"], 1700000100.0)
        self.assertTrue(os.path.exists(os.path.join(self.out, "index.md")))
        self.assertIn("file_gone", read(os.path.join(self.out, "unavailable.md")))
        self.assertEqual(os.listdir(self.dl), [])        # staging folder is emptied

    def test_update_does_not_redownload_and_keeps_unavailable(self):
        self.conv(CID1)
        self.execute(self.runner(), {CID1: "new"}, [item(CID1)])
        self.api.calls.clear()
        c = self.conv(CID1, update=1700009999.0)
        c["title"] = "Renamed"
        self.execute(self.runner(), {CID1: "updated"}, [item(CID1, update=1700009999.0)])
        self.assertEqual([x for x in self.api.calls if x[0] in ("file_link", "save_file")], [])
        e = store.Manifest.load(self.out).get(CID1)
        self.assertEqual(e["title"], "Renamed")
        self.assertIn("_Hello__", e["folder"])            # folder keeps its original leaf name

    def test_identical_file_in_two_conversations_downloaded_once(self):
        self.conv(CID1)
        self.api.convs[CID2] = make_conv(CID2, messages=[msg("user", [IMG], ctype="multimodal_text")])
        self.execute(self.runner(), {CID1: "new", CID2: "new"}, [item(CID1), item(CID2)])
        self.assertEqual(sum(1 for x in self.api.calls if x[0] == "save_file"), 1)
        m = store.Manifest.load(self.out)
        for cid in (CID1, CID2):
            self.assertTrue(os.path.exists(os.path.join(self.out, m.get(cid)["files"][0]["path"])))

    def test_move_to_project_archive_and_delete(self):
        self.conv(CID1)
        self.execute(self.runner(), {CID1: "new"}, [item(CID1)])
        old_folder = store.Manifest.load(self.out).get(CID1)["folder"]
        # moved into a project and archived on the server; no refetch needed
        it = item(CID1, archived=True, project="g-p-1")
        self.execute(self.runner(), {}, [it], sync=[{"id": CID1, "change": "moved+archived"}])
        e = store.Manifest.load(self.out).get(CID1)
        self.assertTrue(e["folder"].startswith(os.path.join("projects", "Cooking")))
        self.assertEqual(e["path_history"][0]["folder"], old_folder)
        self.assertTrue(e["archived"])
        self.assertTrue(os.path.exists(os.path.join(self.out, e["files"][0]["path"])))   # artifacts moved too
        self.assertFalse(os.path.exists(os.path.join(self.out, "no-project")))           # emptied dir pruned
        md = read(os.path.join(self.out, e["folder"], "conversation.md"))
        self.assertIn("Archived: yes", md)
        self.assertIn("Project: Cooking", md)
        self.assertIn("be tasty", read(os.path.join(self.out, "projects", "Cooking", "instructions.md")))
        pj = json.loads(read(os.path.join(self.out, "projects", "Cooking", "project.json")))
        self.assertEqual(pj["files"][0]["status"], "not downloaded")
        # deleted on the server: nothing removed, only flagged
        self.execute(self.runner(), {}, [], deleted=[CID1])
        e = store.Manifest.load(self.out).get(CID1)
        self.assertTrue(e["deleted_on_server"])
        self.assertTrue(os.path.exists(os.path.join(self.out, e["folder"], "conversation.json")))
        self.assertIn("Deleted on server: yes", read(os.path.join(self.out, e["folder"], "conversation.md")))

    def test_transient_file_error_recorded_and_retried(self):
        self.conv(CID1)
        self.api.links["file_ok1"] = ("error", "HTTP 503")
        self.execute(self.runner(), {CID1: "new"}, [item(CID1)])
        e = store.Manifest.load(self.out).get(CID1)
        self.assertEqual({f["id"]: f["status"] for f in e["files"]}["file_ok1"], "error")
        self.api.links["file_ok1"] = ("ok", "u1")
        self.execute(self.runner(), {CID1: "incomplete"}, [item(CID1)])
        e = store.Manifest.load(self.out).get(CID1)
        self.assertEqual({f["id"]: f["status"] for f in e["files"]}["file_ok1"], "ok")

    def test_no_files_flag(self):
        self.conv(CID1)
        self.execute(self.runner(want_files=False), {CID1: "new"}, [item(CID1)])
        self.assertEqual([x for x in self.api.calls if x[0] == "save_file"], [])
        e = store.Manifest.load(self.out).get(CID1)
        self.assertEqual({f["status"] for f in e["files"]}, {"skipped"})

    def test_failure_does_not_stop_others_and_is_resumable(self):
        self.conv(CID1)
        self.api.convs[CID2] = make_conv(CID2)
        self.api.fail_convs.add(CID1)
        fails = self.execute(self.runner(), {CID1: "new", CID2: "new"}, [item(CID1), item(CID2)])
        self.assertEqual([f[0] for f in fails], [CID1])
        m = store.Manifest.load(self.out)
        self.assertIsNone(m.get(CID1))
        self.assertIsNotNone(m.get(CID2))

    def test_rebuild_from_disk(self):
        self.conv(CID1)
        r = self.runner()
        self.execute(r, {CID1: "new"}, [item(CID1)])
        e = r.manifest.get(CID1)
        path = os.path.join(self.out, e["folder"], "conversation.md")
        os.unlink(path)
        Runner(self.out, None, {}, log=silent).rerender(Runner(self.out, None, {}, log=silent).manifest.get(CID1))
        self.assertTrue(os.path.exists(path))

    def test_lock(self):
        with Lock(self.out):
            with self.assertRaises(LockError):
                with Lock(self.out):
                    pass
        with Lock(self.out):       # released afterwards
            pass


if __name__ == "__main__":
    unittest.main()
