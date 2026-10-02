"""ChatGPT's internal web API, called from inside the logged-in tab.

Requests are made by page JavaScript (``fetch`` with the page's own access token)
so that they look like the site's own traffic. Nothing here stores the token:
it lives only in a page variable.

The API is undocumented and may change; every method reports HTTP status codes
instead of guessing, and callers fail loudly on unexpected shapes.
"""
from __future__ import annotations

import glob
import json
import os
import time

from .cdp import AgentBrowser, CdpError
from .plan import iso_to_epoch

BACKOFF = (15, 30, 60, 60, 120)

_FETCH_JS = """(async()=>{
  const path=%(path)s;
  if(!window.__cbt){const r=await fetch('/api/auth/session');const j=await r.json();window.__cbt=j.accessToken||null}
  if(!window.__cbt) return {s:401,t:''};
  let r=await fetch(path,{headers:{Authorization:'Bearer '+window.__cbt}});
  if(r.status===401){window.__cbt=null;
    const r2=await fetch('/api/auth/session');const j=await r2.json();window.__cbt=j.accessToken||null;
    if(!window.__cbt) return {s:401,t:''};
    r=await fetch(path,{headers:{Authorization:'Bearer '+window.__cbt}})}
  return {s:r.status,t:await r.text()}
})()"""

_SAVE_JS = """(async()=>{
  const r=await fetch(%(url)s,{headers:{Authorization:'Bearer '+window.__cbt}});
  if(!r.ok) return {s:r.status};
  const b=await r.blob();
  const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=%(name)s;
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),30000);
  return {s:200,size:b.size}
})()"""

_SESSION_JS = """(async()=>{const r=await fetch('/api/auth/session');const j=await r.json();
  return {logged_in:!!j.accessToken,email:(j.user&&j.user.email)||null}})()"""


class ApiError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class ChatGPT:
    def __init__(self, browser: AgentBrowser, sleep=time.sleep, log=lambda m: None):
        self.ab = browser
        self.sleep = sleep
        self.log = log

    # ----- tab / session ----------------------------------------------------
    def ensure_tab(self):
        try:
            url = self.ab.current_url()
        except CdpError:
            url = ""
        if not str(url).startswith("https://chatgpt.com"):
            self.ab.open("https://chatgpt.com/")

    def session(self):
        self.ensure_tab()
        return self.ab.eval_json(_SESSION_JS)

    # ----- low level --------------------------------------------------------
    def get(self, path, retries=len(BACKOFF)):
        """GET ``path``; retries throttling (429) and 5xx with backoff. Returns ``(status, text)``."""
        for attempt in range(retries + 1):
            res = self.ab.eval_json(_FETCH_JS % {"path": json.dumps(path)}, timeout=180)
            st = res["s"]
            if st == 429 or st >= 500:
                if attempt < retries:
                    wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                    self.log("  server busy (%d) - waiting %ds" % (st, wait))
                    self.sleep(wait)
                    continue
            return st, res["t"]
        return st, res["t"]

    def get_json(self, path):
        st, text = self.get(path)
        if st != 200:
            raise ApiError("GET %s -> HTTP %s" % (path.split("?")[0], st), st)
        try:
            return json.loads(text)
        except ValueError:
            raise ApiError("GET %s returned non-JSON" % path.split("?")[0], st)

    # ----- domain -----------------------------------------------------------
    def list_conversations(self, page=100):
        """Every conversation (active, archived, and those only listed under a project).

        Normalised dicts with epoch times. The main list omits the conversations of some
        projects, so the per-project lists are merged in as well.
        """
        out = {}
        for archived in (False, True):
            offset = 0
            while True:
                data = self.get_json("/backend-api/conversations?offset=%d&limit=%d&order=updated&is_archived=%s"
                                     % (offset, page, "true" if archived else "false"))
                items = data.get("items")
                if items is None:
                    raise ApiError("conversation list has an unexpected shape (no 'items')")
                if not items:
                    break
                for c in items:
                    out[c["id"]] = {"id": c["id"], "title": c.get("title"),
                                    "create_time": iso_to_epoch(c.get("create_time")),
                                    "update_time": iso_to_epoch(c.get("update_time")),
                                    "archived": archived, "project_id": c.get("gizmo_id")}
                offset += len(items)
        for pid in self.project_ids({it["project_id"] for it in out.values() if it["project_id"]}):
            cursor = 0
            while cursor is not None:
                data = self.get_json("/backend-api/gizmos/%s/conversations?cursor=%s" % (pid, cursor))
                for c in data.get("items") or []:
                    cur = out.get(c["id"])
                    if cur is None:
                        out[c["id"]] = {"id": c["id"], "title": c.get("title"),
                                        "create_time": iso_to_epoch(c.get("create_time")),
                                        "update_time": iso_to_epoch(c.get("update_time")),
                                        "archived": bool(c.get("is_archived")), "project_id": pid}
                    elif not cur["project_id"]:
                        cur["project_id"] = pid
                cursor = data.get("cursor")
        return list(out.values())

    def project_ids(self, known):
        """Project ids: those seen on conversations plus the sidebar's project list."""
        ids = set(known)
        try:
            side = self.get_json("/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0&owned_only=true")
            for it in side.get("items") or []:
                gid = ((it.get("gizmo") or {}).get("gizmo") or {}).get("id")
                if gid:
                    ids.add(gid)
        except ApiError as e:
            self.log("warning: could not read the project sidebar (%s)" % e)
        return sorted(ids)

    def project(self, pid):
        j = self.get_json("/backend-api/gizmos/%s" % pid)
        g = j.get("gizmo") or {}
        return {"id": pid, "name": (g.get("display") or {}).get("name") or pid, "raw": j}

    def conversation(self, cid):
        return self.get_json("/backend-api/conversation/%s" % cid)

    def file_link(self, fid, cid):
        """``("ok", url)`` | ``("unavailable", reason)`` | ``("error", reason)`` for transient failures."""
        st, text = self.get("/backend-api/files/download/%s?conversation_id=%s&inline=false" % (fid, cid))
        if st == 200:
            try:
                j = json.loads(text)
            except ValueError:
                return "error", "unreadable response"
            if j.get("download_url"):
                return "ok", j["download_url"]
            if j.get("error_code") == "file_not_found":
                return "unavailable", "file not found on server (no download link)"
            return "error", "no download link: %s" % (j.get("error_code") or "unknown")
        if st == 404:
            return "unavailable", "file not found on server (404)"
        if st == 403:
            return "unavailable", "belongs to another user (403)"
        if st == 422:
            return "unavailable", "not a downloadable file id (422)"
        return "error", "HTTP %s" % st

    def save_file(self, url, basename, downloads_dir, timeout=180):
        """Have the browser download ``url`` into ``downloads_dir`` as ``basename``.

        Returns the local path once the file is complete. Requires an active download holder.
        """
        for f in glob.glob(os.path.join(downloads_dir, glob.escape(basename) + "*")):
            os.unlink(f)
        res = self.ab.eval_json(_SAVE_JS % {"url": json.dumps(url), "name": json.dumps(basename)}, timeout=timeout)
        if res["s"] != 200:
            return None, "HTTP %s" % res["s"]
        deadline = time.time() + timeout
        target = os.path.join(downloads_dir, basename)
        last, stable = -1, 0
        while time.time() < deadline:
            partial = glob.glob(os.path.join(downloads_dir, glob.escape(basename) + "*.crdownload")) + \
                      glob.glob(os.path.join(downloads_dir, "*.tmp"))
            if os.path.exists(target) and not partial:
                size = os.path.getsize(target)
                stable = stable + 1 if size == last else 0
                last = size
                if stable >= 1 and size == res.get("size", size):
                    return target, None
            time.sleep(0.5)
        return None, "download did not finish (is the download holder running? Chrome may be asking for permission)"
