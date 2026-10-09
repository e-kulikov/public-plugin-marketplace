"""Drive the Teams web client: open a chat, collect messages, download files, read meeting transcripts."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from urllib.parse import quote, unquote

from . import pagejs
from .cdp import AgentBrowser, CdpError, eval_in_target, list_targets

TEAMS_ORIGIN = "https://teams.cloud.microsoft"
THREAD_RE = re.compile(r"(19:[A-Za-z0-9_\-=.]+@thread\.(?:v2|skype|tacv2)|19:[A-Za-z0-9_\-=.]+@unq\.gbl\.spaces|19:[0-9a-f\-]+_[0-9a-f\-]+@unq\.gbl\.spaces)")


class TeamsError(RuntimeError):
    pass


class NotLoggedIn(TeamsError):
    pass


def parse_chat_ref(ref):
    """A Teams chat link (old or new host, any of its forms) or a bare ``19:...@thread.v2`` id -> thread id."""
    s = unquote(unquote(ref.strip()))
    m = THREAD_RE.search(s)
    if not m:
        raise TeamsError("cannot find a chat id (19:...@thread.v2) in %r" % ref)
    return m.group(1)


def deep_link(thread_id):
    return "%s/l/chat/%s/conversations?context=%s" % (TEAMS_ORIGIN, thread_id, quote('{"contextType":"chat"}', safe=""))


class Teams:
    def __init__(self, cdp_url, log=print):
        self.cdp_url = cdp_url
        self.ab = AgentBrowser(cdp_url)
        self.log = log

    # -- page state -------------------------------------------------------------------------------
    def info(self):
        return self.ab.eval_json(pagejs.PAGE_INFO)

    def check_session(self):
        i = self.info()
        host = i.get("host") or ""
        signing_in = "login." in host or "microsoftonline" in host
        return {"logged_in": bool(i.get("app")) and not signing_in, "url": i.get("url"), "host": host}

    def install(self):
        self.ab.eval_json("%s" % pagejs.INSTALL.strip().rstrip(";"))

    # -- opening a chat ---------------------------------------------------------------------------
    def open_chat(self, thread_id, timeout=90):
        """Navigate to the chat (skipping the "desktop app" interstitial) and wait until messages render."""
        self.ab.navigate(deep_link(thread_id))
        t0 = time.time()
        clicked_at = 0
        while time.time() - t0 < timeout:
            time.sleep(2)
            try:
                i = self.info()
            except CdpError:
                continue
            host = i.get("host") or ""
            if "login.microsoftonline.com" in host or "login.live.com" in host:
                raise NotLoggedIn("Teams asks to sign in (%s)" % host)
            if i.get("launcher") and time.time() - clicked_at > 8:
                self.ab.eval_json(pagejs.CLICK_LAUNCHER)   # "Use the web app instead"
                clicked_at = time.time()
                continue
            if i.get("viewport") and i.get("title") and (i.get("msgs") or time.time() - t0 > 25):
                time.sleep(1.5)
                return self.info()
        raise TeamsError("the chat did not open within %ds (url: %s)" % (timeout, (self.info().get("url") or "")[:100]))

    # -- messages ---------------------------------------------------------------------------------
    def collect_messages(self, from_ms=None, max_seconds=900):
        """Scroll the history up until ``from_ms`` (or the very first message) and return all items seen."""
        self.install()
        t0 = time.time()
        while True:
            r = self.ab.eval_json("window.__tb.run(%s, 60000)" % (int(from_ms) if from_ms else "null"), timeout=120)
            self.log("  history: %s messages loaded%s" % (r.get("count"), " (done: %s)" % r["reason"] if r.get("done") else ""))
            if r.get("done"):
                break
            if time.time() - t0 > max_seconds:
                self.log("  stopped loading history after %ds" % max_seconds)
                break
        items, i = [], 0
        while True:
            part = self.ab.eval_json("window.__tb.slice(%d, 200)" % i)
            if not part:
                break
            items += part
            i += len(part)
        return items

    # -- files ------------------------------------------------------------------------------------
    def download_attachment(self, mid, idx, staging, timeout=120):
        """Click Download for the idx-th file card of message ``mid``; return the new file in ``staging``."""
        if not self.ab.eval_json("window.__tb.find(%s)" % json.dumps(str(mid)), timeout=180):
            return None, "message is not reachable in the list"
        before = set(os.listdir(staging))
        res = self.ab.eval_json("window.__tb.download(%s, %d)" % (json.dumps(str(mid)), idx))
        if res != "ok":
            return None, "no Download action for this item (%s)" % res
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(1)
            new = [f for f in set(os.listdir(staging)) - before if not f.endswith((".crdownload", ".tmp"))]
            if new:
                p = os.path.join(staging, new[0])
                s1 = os.path.getsize(p)
                time.sleep(1)
                if os.path.getsize(p) == s1:
                    return p, None
        return None, "download did not finish in %ds" % timeout

    # -- meetings ---------------------------------------------------------------------------------
    def meeting_options(self):
        """Open the Recap tab; return the list of meeting labels ([] when the chat has no recap)."""
        r = self.ab.eval_json(pagejs.OPEN_RECAP)
        if r != "ok":
            return []
        res = self.ab.eval_json(pagejs.RECAP_OPTIONS, timeout=60)
        if res.get("error"):
            return []
        return res["options"] or ([res["current"]] if res.get("current") else [])

    def _transcript_target(self, timeout=45):
        t0 = time.time()
        while time.time() - t0 < timeout:
            cands = []
            for t in list_targets(self.cdp_url):
                if t.get("type") != "iframe":
                    continue
                try:
                    probe = json.loads(eval_in_target(self.cdp_url, t["id"], pagejs.TRANSCRIPT_PROBE, timeout=15))
                except (CdpError, OSError, ValueError):
                    continue
                if probe.get("zone") and probe.get("size", 0) > 0:
                    cands.append(t["id"])
            if cands:
                return cands[-1]
            time.sleep(1.5)
        return None

    def read_transcript(self, index):
        """Select meeting ``index`` in the Recap tab and return its transcript entries (or None, reason)."""
        if self.ab.eval_json(pagejs.SELECT_RECAP_OPTION % index, timeout=60) not in ("ok",):
            # a single meeting may have no list of options: the selector then only shows the current one
            if index != 0:
                return None, "cannot select the meeting"
        time.sleep(2)
        if self.ab.eval_json(pagejs.OPEN_TRANSCRIPT_TAB, timeout=60) != "ok":
            return None, "no Transcript tab (no transcript was recorded)"
        tid = self._transcript_target()
        if not tid:
            return None, "transcript did not load (not recorded, or no access)"
        data = json.loads(eval_in_target(self.cdp_url, tid, pagejs.TRANSCRIPT_COLLECT, timeout=600))
        if data.get("error"):
            return None, data["error"]
        got, size = len(data["entries"]), data.get("size") or 0
        if size and got < size:
            return data["entries"], "incomplete: %d of %d entries" % (got, size)
        return data["entries"], None

    def back_to_chat(self):
        self.ab.eval_json(pagejs.OPEN_CHAT_TAB)
        time.sleep(1.5)


def move_into(src, dst_dir, name):
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name)
    if os.path.exists(dst):
        base, ext = os.path.splitext(name)
        n = 1
        while os.path.exists(dst):
            dst = os.path.join(dst_dir, "%s (%d)%s" % (base, n, ext))
            n += 1
    shutil.move(src, dst)
    return dst
