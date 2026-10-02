"""Execute a backup plan, one conversation at a time.

For each conversation: fetch JSON -> place files -> render Markdown -> verify the
message count -> record it in ``manifest.json``. The manifest is the checkpoint, so
an interrupted run simply continues with what is not recorded yet.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import shutil
import time

from . import render, store
from .api import ApiError

GAP_BETWEEN_FILES = 1.0
GAP_BETWEEN_CONVERSATIONS = 2.0


class LockError(RuntimeError):
    pass


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Lock:
    """One backup per folder at a time (pid lock file; stale locks are taken over)."""

    def __init__(self, out_dir):
        self.path = os.path.join(out_dir, ".chatgpt-backup.lock")

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            try:
                pid = int(open(self.path).read().strip())
                os.kill(pid, 0)
                raise LockError("another backup is running in this folder (pid %d)" % pid)
            except (ValueError, OSError):
                pass  # stale
        with open(self.path, "w") as f:
            f.write(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Runner:
    def __init__(self, out_dir, api, projects, downloads_dir=None, want_files=True,
                 log=print, sleep=time.sleep, manifest=None):
        self.out = out_dir
        self.api = api
        self.projects = projects          # pid -> {"id","name","raw"}
        self.downloads = downloads_dir
        self.want_files = want_files
        self.log = log
        self.sleep = sleep
        self.manifest = manifest or store.Manifest.load(out_dir)
        self.failures = []                # (cid, title, message)
        self.touched_projects = set()
        self._index_files()

    # ----- helpers ----------------------------------------------------------
    def _index_files(self):
        self.file_index = {}
        for e in self.manifest.conversations:
            for f in e.get("files") or []:
                if f.get("status") == "ok" and f.get("path"):
                    self.file_index.setdefault(f["id"], f["path"])

    def pname(self, pid):
        if not pid:
            return None
        p = self.projects.get(pid)
        return p["name"] if p else pid

    def _folder_for(self, old, cid, conv, pid):
        pname = self.pname(pid)
        if old:
            target = store.retarget(old["folder"], pname, pid)
            if target != old["folder"]:
                self.log("  moving %s -> %s" % (old["folder"], target))
                old_parent = os.path.dirname(old["folder"])
                store.move_folder(self.out, old["folder"], target)
                store.prune_empty_dirs(self.out, old_parent)
                hist = old.setdefault("path_history", [])
                hist.append({"folder": old["folder"], "until": _now_iso()})
                self._index_files_after_move(old["folder"], target)
            return target
        return store.new_folder(conv.get("create_time"), conv.get("title"), cid, pname, pid)

    def _index_files_after_move(self, old_folder, new_folder):
        e = None
        for c in self.manifest.conversations:
            if c["folder"] == old_folder:
                e = c
        if not e:
            return
        for f in e.get("files") or []:
            if f.get("path", "").startswith(old_folder + os.sep):
                f["path"] = new_folder + f["path"][len(old_folder):]
        e["folder"] = new_folder
        self._index_files()

    # ----- files --------------------------------------------------------------
    def _place_files(self, cid, conv, folder, old):
        refs = render.file_refs(conv)
        prev = {f["id"]: f for f in (old or {}).get("files", [])}
        art_dir = os.path.join(self.out, folder, "artifacts")
        records = []
        for fid, ref in refs.items():
            rec = {"id": fid, "kind": ref["kind"], "name": ref["name"], "mime": ref["mime"]}
            records.append(rec)
            existing = glob.glob(os.path.join(art_dir, glob.escape(fid) + "__*"))
            why = render.unfetchable_reason(fid, ref["kind"])
            p = prev.get(fid)
            if existing:
                rec.update(status="ok", path=os.path.join(folder, "artifacts", os.path.basename(existing[0])))
            elif why:
                rec.update(status="unavailable", reason=why)
            elif p and p.get("status") == "unavailable":
                rec.update(status="unavailable", reason=p.get("reason"))
            elif not self.want_files:
                rec.update(status="skipped", reason="skipped (--no-files)")
            elif fid in self.file_index and os.path.exists(os.path.join(self.out, self.file_index[fid])):
                src = os.path.join(self.out, self.file_index[fid])
                os.makedirs(art_dir, exist_ok=True)
                dst = os.path.join(art_dir, os.path.basename(src))
                shutil.copy2(src, dst)
                rec.update(status="ok", path=os.path.join(folder, "artifacts", os.path.basename(dst)))
            else:
                self._download(cid, ref, rec, art_dir, folder)
                self.sleep(GAP_BETWEEN_FILES)
        return records

    def _download(self, cid, ref, rec, art_dir, folder):
        st, val = self.api.file_link(ref["id"], cid)
        if st != "ok":
            rec.update(status=st, reason=val)
            return
        name = render.download_basename(ref["id"], ref["name"], ref["mime"])
        path, err = self.api.save_file(val, name, self.downloads)
        if not path:
            rec.update(status="error", reason=err)
            return
        final = os.path.basename(path)
        if not os.path.splitext(final)[1]:
            final += render.sniff_ext(path)
        os.makedirs(art_dir, exist_ok=True)
        dst = os.path.join(art_dir, final)
        shutil.move(path, dst)
        rec.update(status="ok", path=os.path.join(folder, "artifacts", final))
        self.file_index.setdefault(ref["id"], rec["path"])

    # ----- rendering ------------------------------------------------------------
    def _file_info(self, files):
        by_id = {f["id"]: f for f in files}

        def info(fid):
            f = by_id.get(fid)
            if not f:
                return "unavailable", "file reference not recorded", None
            if f.get("status") == "ok":
                return "ok", None, os.path.basename(f["path"])
            return f.get("status", "unavailable"), f.get("reason") or "unavailable", None
        return info

    def _write_md(self, entry, conv):
        meta = {"id": entry["id"], "project": entry.get("project"), "archived": entry.get("archived"),
                "deleted": entry.get("deleted_on_server")}
        text, count = render.render_markdown(conv, meta, self._file_info(entry.get("files") or []))
        store.atomic_write_text(os.path.join(self.out, entry["folder"], "conversation.md"), text)
        return count

    def rerender(self, entry):
        """Re-generate conversation.md from the stored conversation.json (no network)."""
        with open(os.path.join(self.out, entry["folder"], "conversation.json"), encoding="utf-8") as f:
            conv = json.load(f)
        entry["messages"] = self._write_md(entry, conv)
        return conv

    # ----- one conversation --------------------------------------------------------
    def backup_one(self, cid, item, reason):
        conv = self.api.conversation(cid)
        pid = conv.get("gizmo_id") or (item or {}).get("project_id")
        old = self.manifest.get(cid)
        folder = self._folder_for(old, cid, conv, pid)
        old = self.manifest.get(cid)  # entry may have been updated by a move
        os.makedirs(os.path.join(self.out, folder), exist_ok=True)
        store.atomic_write_json(os.path.join(self.out, folder, "conversation.json"), conv)
        files = self._place_files(cid, conv, folder, old)
        archived = item["archived"] if item else bool(conv.get("is_archived"))
        entry = {
            "id": cid, "title": conv.get("title"), "project": self.pname(pid), "project_id": pid,
            "archived": archived, "created": render.ts(conv.get("create_time")),
            "updated": render.ts(conv.get("update_time")), "create_time": conv.get("create_time"),
            "update_time": conv.get("update_time"),
            "list_update_time": item["update_time"] if item else conv.get("update_time"),
            "folder": folder, "files": files, "deleted_on_server": False,
            "path_history": (old or {}).get("path_history", []), "backed_up_at": _now_iso(),
        }
        count = self._write_md(entry, conv)
        entry["messages"] = count
        expected = render.expected_message_count(conv)
        if count != expected:
            entry["warnings"] = ["message count mismatch: rendered %d, expected %d" % (count, expected)]
            self.failures.append((cid, conv.get("title"), entry["warnings"][0]))
        self.manifest.upsert(entry)
        self._index_files()
        self.manifest.data["last_backup"] = {"at": _now_iso(), "freshest_update_time": self.manifest.freshest_update_time()}
        self.manifest.save()
        if pid:
            self.touched_projects.add(pid)
        return entry

    # ----- metadata-only changes ----------------------------------------------------
    def sync_one(self, cid, item, change):
        e = self.manifest.get(cid)
        if e is None:
            return
        if change == "restored" or "restored" in change:
            e["deleted_on_server"] = False
        if item is not None:
            e["archived"] = bool(item["archived"])
            pid = item.get("project_id")
            folder = self._folder_for(e, cid, {"title": e.get("title"), "create_time": e.get("create_time")}, pid)
            e = self.manifest.get(cid)
            e["project_id"] = pid
            e["project"] = self.pname(pid)
            e["folder"] = folder
            e["list_update_time"] = item["update_time"]
            if pid:
                self.touched_projects.add(pid)
        self.rerender(e)
        self.manifest.save()

    def mark_deleted(self, cid):
        e = self.manifest.get(cid)
        if e is None:
            return
        e["deleted_on_server"] = True
        self.rerender(e)
        self.manifest.save()

    # ----- projects --------------------------------------------------------------------
    def write_project(self, pid):
        p = self.projects.get(pid)
        if not p:
            return
        raw = p.get("raw") or {}
        g = raw.get("gizmo") or {}
        d = os.path.join(self.out, store.project_dir(p["name"], pid))
        os.makedirs(d, exist_ok=True)
        rec = {"id": pid, "name": p["name"], "description": (g.get("display") or {}).get("description"),
               "instructions": g.get("instructions"),
               "conversations": [e["id"] for e in self.manifest.conversations if e.get("project_id") == pid],
               "files": [{"name": f.get("name"), "file_id": f.get("file_id"), "type": f.get("type"),
                          "size": f.get("size"), "created_at": f.get("created_at"),
                          "status": "not downloaded",
                          "reason": "the server refuses downloads of project files (403/404/500)"}
                         for f in raw.get("files") or []],
               "raw": raw}
        store.atomic_write_json(os.path.join(d, "project.json"), rec)
        ins = (g.get("instructions") or "").strip()
        if ins:
            store.atomic_write_text(os.path.join(d, "instructions.md"),
                                    "# %s: instructions\n\n%s\n" % (p["name"], g["instructions"]))

    # ----- driver -------------------------------------------------------------------------
    def execute(self, plan, listing_by_id):
        todo = list(plan["fetch"].items())
        total = len(todo)
        try:
            for cid in plan["deleted"]:
                self.mark_deleted(cid)
            for s in plan["sync"]:
                try:
                    self.sync_one(s["id"], listing_by_id.get(s["id"]), s["change"])
                except Exception as e:  # keep going: one broken folder must not stop the backup
                    self.failures.append((s["id"], (self.manifest.get(s["id"]) or {}).get("title"), "sync failed: %s" % e))
            for i, (cid, reason) in enumerate(todo, 1):
                item = listing_by_id.get(cid)
                title = (item or {}).get("title") or cid
                try:
                    e = self.backup_one(cid, item, reason)
                    bad = sum(1 for f in e["files"] if f["status"] != "ok")
                    self.log("[%d/%d] %s  %r  (%s)  msgs=%d files=%d%s" % (
                        i, total, cid, title, reason, e["messages"], len(e["files"]),
                        " (%d not saved)" % bad if bad else ""))
                except ApiError as ex:
                    self.failures.append((cid, title, str(ex)))
                    self.log("[%d/%d] %s  FAILED: %s" % (i, total, cid, ex))
                self.sleep(GAP_BETWEEN_CONVERSATIONS)
        finally:
            for pid in self.touched_projects:
                try:
                    self.write_project(pid)
                except Exception as ex:
                    self.failures.append((pid, "project", "project.json failed: %s" % ex))
            self.manifest.save()
            store.write_top_level(self.out, self.manifest)
        return self.failures
