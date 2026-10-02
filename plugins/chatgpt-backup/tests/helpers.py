"""Synthetic ChatGPT payloads and a fake API for offline tests (no real conversations)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from chatgpt_backup.api import ApiError  # noqa: E402


def msg(role, parts, ctype="text", t=1700000000.0, **meta):
    return {"author": {"role": role}, "create_time": t,
            "content": {"content_type": ctype, "parts": parts}, "metadata": meta}


def make_conv(cid, title="Hello", messages=None, project=None, archived=False,
              create=1700000000.0, update=1700000100.0, extra_nodes=None):
    """A linear conversation: root -> m1 -> m2 ... (current_node = last)."""
    messages = messages if messages is not None else [msg("user", ["hi"]), msg("assistant", ["hello"])]
    mapping = {"root": {"id": "root", "parent": None, "children": [], "message": None}}
    prev = "root"
    for i, m in enumerate(messages, 1):
        nid = "n%d" % i
        mapping[nid] = {"id": nid, "parent": prev, "children": [], "message": m}
        mapping[prev]["children"].append(nid)
        prev = nid
    mapping.update(extra_nodes or {})
    return {"conversation_id": cid, "title": title, "create_time": create, "update_time": update,
            "gizmo_id": project, "is_archived": archived, "default_model_slug": "gpt-test",
            "mapping": mapping, "current_node": prev}


def item(cid, title="Hello", update=1700000100.0, archived=False, project=None, create=1700000000.0):
    return {"id": cid, "title": title, "create_time": create, "update_time": update,
            "archived": archived, "project_id": project}


class FakeApi:
    """Stands in for chatgpt_backup.api.ChatGPT."""

    def __init__(self):
        self.convs = {}
        self.links = {}       # file_id -> ("ok", url) | ("unavailable", reason) | ("error", reason)
        self.blobs = {}       # url -> bytes
        self.calls = []
        self.fail_convs = set()

    def conversation(self, cid):
        self.calls.append(("conversation", cid))
        if cid in self.fail_convs:
            raise ApiError("GET conversation -> HTTP 500", 500)
        return self.convs[cid]

    def file_link(self, fid, cid):
        self.calls.append(("file_link", fid))
        return self.links.get(fid, ("unavailable", "file not found on server (404)"))

    def save_file(self, url, basename, downloads_dir, timeout=0):
        self.calls.append(("save_file", basename))
        os.makedirs(downloads_dir, exist_ok=True)
        path = os.path.join(downloads_dir, basename)
        with open(path, "wb") as f:
            f.write(self.blobs[url])
        return path, None


def write(path, data):
    """Write str/bytes/obj(json) to path, creating folders; closes the file."""
    import json as _json
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f:
            f.write(data)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else _json.dumps(data))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()
