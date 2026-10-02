"""Backup folder layout and ``manifest.json`` (schema 2).

Layout::

    <out>/manifest.json
    <out>/index.md, unavailable.md          (regenerated from the manifest)
    <out>/no-project/<date>_<slug>__<id>/conversation.{md,json}, artifacts/
    <out>/projects/<project>/<date>_<slug>__<id>/...
    <out>/projects/<project>/project.json, instructions.md

The manifest is the checkpoint: a conversation is recorded only after it was
fully written, so an interrupted run resumes at the first missing entry.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from .render import safe, slug, ts

SCHEMA = 2


class Manifest:
    def __init__(self, path):
        self.path = path
        self.data = {"schema": SCHEMA, "last_backup": None, "conversations": []}
        self._by_id = {}

    # ----- persistence -------------------------------------------------
    @classmethod
    def load(cls, out_dir):
        m = cls(os.path.join(out_dir, "manifest.json"))
        if os.path.exists(m.path):
            with open(m.path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):  # legacy (schema 1): a bare list
                raw = {"schema": 1, "last_backup": None, "conversations": raw}
            m.data = raw
        m._reindex()
        return m

    def _reindex(self):
        self._by_id = {c["id"]: c for c in self.data["conversations"]}

    @property
    def schema(self):
        return self.data.get("schema", 1)

    @property
    def conversations(self):
        return self.data["conversations"]

    def get(self, cid):
        return self._by_id.get(cid)

    def upsert(self, entry):
        old = self._by_id.get(entry["id"])
        if old is None:
            self.data["conversations"].append(entry)
        else:
            self.data["conversations"][self.data["conversations"].index(old)] = entry
        self._by_id[entry["id"]] = entry

    def save(self):
        self.data["schema"] = SCHEMA
        _atomic_write_json(self.path, self.data)

    def freshest_update_time(self):
        times = [c.get("update_time") or 0 for c in self.conversations]
        return max(times) if times else 0


def _atomic_write_json(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_text(path, text):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path, obj):
    _atomic_write_json(path, obj)


# ----- layout ---------------------------------------------------------------

def project_dir(project_name, project_id):
    return os.path.join("projects", safe(project_name or project_id, 60) or "project")


def base_dir(project_name, project_id):
    return project_dir(project_name, project_id) if project_id else "no-project"


def new_folder(create_time, title, cid, project_name, project_id):
    date = ts(create_time)[:10] or "undated"
    return os.path.join(base_dir(project_name, project_id), "%s_%s__%s" % (date, slug(title), cid))


def retarget(old_folder, project_name, project_id):
    """Same leaf folder name (stable on title changes), possibly another project dir."""
    return os.path.join(base_dir(project_name, project_id), os.path.basename(old_folder))


def move_folder(out_dir, old_rel, new_rel):
    """Move a conversation folder; ``git mv`` when the backup is a git repo."""
    src, dst = os.path.join(out_dir, old_rel), os.path.join(out_dir, new_rel)
    if old_rel == new_rel or not os.path.exists(src):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        raise FileExistsError(dst)
    if os.path.isdir(os.path.join(out_dir, ".git")):
        r = subprocess.run(["git", "-C", out_dir, "mv", old_rel, new_rel],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return True
    shutil.move(src, dst)
    return True


def prune_empty_dirs(out_dir, rel):
    """Remove an emptied project directory (only if it holds nothing at all)."""
    p = os.path.join(out_dir, rel)
    while p != out_dir and os.path.isdir(p) and not os.listdir(p):
        os.rmdir(p)
        p = os.path.dirname(p)


# ----- migration of a schema-1 manifest ----------------------------------------

def migrate_entry(out_dir, e):
    """Fill schema-2 fields of a legacy entry from its ``conversation.json``."""
    if "update_time" in e and "create_time" in e:
        return e
    conv_path = os.path.join(out_dir, e["folder"], "conversation.json")
    if os.path.exists(conv_path):
        with open(conv_path, encoding="utf-8") as f:
            conv = json.load(f)
        e["create_time"] = conv.get("create_time")
        e["update_time"] = conv.get("update_time")
        e.setdefault("project_id", conv.get("gizmo_id"))
    e.setdefault("deleted_on_server", False)
    e.setdefault("path_history", [])
    return e


def migrate(out_dir, manifest):
    """Upgrade schema 1 -> 2 in memory. Returns the number of entries touched."""
    n = 0
    for e in manifest.conversations:
        before = dict(e)
        migrate_entry(out_dir, e)
        if e != before:
            n += 1
    return n


# ----- generated top-level docs -------------------------------------------------

def render_index(manifest):
    groups = {}
    for m in manifest.conversations:
        groups.setdefault(m.get("project") or "(no project)", []).append(m)
    lines = ["# ChatGPT archive", "", "Conversations: %d" % len(manifest.conversations), ""]
    for g in sorted(groups, key=lambda x: (x == "(no project)", x)):
        lines += ["## %s (%d)" % (g, len(groups[g])), "",
                  "| Date | Title | Msgs | Files | |", "|---|---|---|---|---|"]
        for m in sorted(groups[g], key=lambda x: x.get("created") or ""):
            files = m.get("files") or []
            bad = sum(1 for f in files if f.get("status") != "ok")
            title = (m.get("title") or "(untitled)").replace("|", "\\|").replace("\n", " ").strip()
            flags = ", ".join(x for x, on in (("archived", m.get("archived")),
                                             ("deleted on server", m.get("deleted_on_server"))) if on)
            lines.append("| %s | [%s](%s/conversation.md) | %s | %d%s | %s |" % (
                (m.get("created") or "")[:10], title, m["folder"].replace(" ", "%20"),
                m.get("messages", ""), len(files), " (%d unavailable)" % bad if bad else "", flags))
        lines.append("")
    return "\n".join(lines)


def render_unavailable(manifest):
    lines = ["# Unavailable files", "",
             "Files referenced in conversations that could not be downloaded.", "",
             "| Conversation | File | Kind | Status | Reason |", "|---|---|---|---|---|"]
    for m in manifest.conversations:
        for f in m.get("files") or []:
            if f.get("status") != "ok":
                lines.append("| %s | `%s` %s | %s | %s | %s |" % (
                    (m.get("title") or "").replace("|", "\\|").replace("\n", " "),
                    f["id"], f.get("name") or "", f.get("kind", ""), f.get("status", ""),
                    f.get("reason") or ""))
    return "\n".join(lines)


def write_top_level(out_dir, manifest):
    atomic_write_text(os.path.join(out_dir, "index.md"), render_index(manifest))
    atomic_write_text(os.path.join(out_dir, "unavailable.md"), render_unavailable(manifest))
