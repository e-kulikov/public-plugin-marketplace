"""One backup run: chat messages + files + meeting transcripts for a single chat."""
from __future__ import annotations

import datetime as dt
import os

from . import store
from .teams import Teams, move_into, parse_chat_ref


def _utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backup_chat(teams: Teams, ref, out_root, frm=None, to=None, files=True, meetings=True, messages=True,
                staging=None, log=print):
    """Back up one chat. ``frm``/``to`` are strings (date or ISO); returns a summary dict."""
    thread = parse_chat_ref(ref)
    log("Opening chat %s ..." % thread)
    page = teams.open_chat(thread)
    title, tz_name = (page.get("title") or "").strip(), page.get("tz") or "UTC"
    tz_off = page.get("off")
    tz = store.get_tz(tz_name, tz_off)
    chat_dir = os.path.join(out_root, store.sanitize(title, "chat"))
    os.makedirs(chat_dir, exist_ok=True)
    meta = store.read_meta(chat_dir)
    meta.update({"chat_id": thread, "title": title, "tz": tz_name, "tz_offset": tz_off})
    store.write_meta(chat_dir, meta)

    lo, hi = store.parse_when(frm, tz), store.parse_when(to, tz, end=True)
    log("Chat: %s  (folder: %s, window: %s .. %s)" % (title, chat_dir, lo or "-", hi or "-"))
    summary = {"chat": title, "dir": chat_dir, "messages": {}, "files": {"saved": 0, "failed": 0}, "meetings": {}}

    if messages:
        _messages(teams, chat_dir, meta, lo, hi, files, staging, summary, log)
    if meetings:
        _meetings(teams, chat_dir, title, tz, tz_name, lo, hi, summary, log)
    return summary


def _messages(teams, chat_dir, meta, lo, hi, files, staging, summary, log):
    jsonl = os.path.join(chat_dir, "chat.jsonl")
    items = teams.collect_messages(from_ms=lo.timestamp() * 1000 if lo else None)
    recs = []
    for it in items:
        ts = it["ts_ms"]
        t = dt.datetime.fromtimestamp(ts / 1000.0, dt.timezone.utc)
        if (lo and t < lo) or (hi and t >= hi):
            continue
        rec = {"id": it["id"], "ts": store.ms_to_iso(ts), "type": it["type"], "author": it.get("author"),
               "author_id": it.get("author_id"), "text": it.get("text") or "",
               "attachments": [{"name": n} for n in it.get("attachments", [])],
               "reactions": it.get("reactions", []), "edited": bool(it.get("edited"))}
        recs.append(rec)
    recs.sort(key=lambda r: (r["ts"], r["id"]))
    log("%d messages in the window." % len(recs))

    known = store.latest_by_id(store.read_jsonl(jsonl))
    tz = store.get_tz(meta.get("tz"), meta.get("tz_offset"))
    for rec in recs:
        old = {a["name"]: a for a in known.get(rec["id"], {}).get("attachments", [])}
        for idx, att in enumerate(rec["attachments"]):
            prev = old.get(att["name"])
            if prev and prev.get("file") and os.path.exists(os.path.join(chat_dir, prev["file"])):
                att["file"] = prev["file"]
                continue
            if not files:
                continue
            local = store.iso_to_dt(rec["ts"]).astimezone(tz)
            path, err = teams.download_attachment(rec["id"], idx, staging)
            if path:
                name = "%s__%s" % (local.strftime("%Y-%m-%d_%H%M"), store.sanitize(att["name"], "file"))
                dst = move_into(path, os.path.join(chat_dir, "attachments"), name)
                att["file"] = "attachments/" + os.path.basename(dst)
                summary["files"]["saved"] += 1
                log("  saved %s" % att["file"])
            else:
                att["error"] = err
                summary["files"]["failed"] += 1
                log("  could not download %s: %s" % (att["name"], err))
    added, revised, same = store.append_new(jsonl, recs, _utc_now_iso())
    total = store.write_md(chat_dir, meta)
    summary["messages"] = {"in_window": len(recs), "added": added, "revised": revised, "unchanged": same, "total_unique": total}
    log("chat.jsonl: +%d new, %d revised, %d unchanged; chat.md has %d messages." % (added, revised, same, total))


def _meetings(teams, chat_dir, title, tz, tz_name, lo, hi, summary, log):
    labels = teams.meeting_options()
    res = summary["meetings"] = {"found": len(labels), "saved": 0, "skipped": 0, "problems": []}
    if not labels:
        log("No meeting recap in this chat.")
        teams.back_to_chat()
        return
    log("%d meeting(s) in the recap." % len(labels))
    for idx, label in enumerate(labels):
        span = store.parse_meeting_label(label, tz)
        if not span:
            res["problems"].append("%s: cannot parse the date (is Teams in English?)" % label)
            log("  cannot parse meeting date %r" % label)
            continue
        start = span[0]
        if (lo and start.astimezone(dt.timezone.utc) < lo) or (hi and start.astimezone(dt.timezone.utc) >= hi):
            res["skipped"] += 1
            continue
        fname = store.meeting_filename(start, title)
        path = os.path.join(chat_dir, "meetings", fname)
        if os.path.exists(path):
            res["skipped"] += 1
            log("  %s already saved" % fname)
            continue
        entries, problem = teams.read_transcript(idx)
        if not entries:
            res["problems"].append("%s: %s" % (label, problem))
            log("  %s: %s" % (label, problem))
            continue
        if problem:   # incomplete: keep what we have but say so
            res["problems"].append("%s: %s" % (label, problem))
            log("  WARNING %s: %s" % (label, problem))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(store.render_transcript(entries, title, label, start, tz_name))
        res["saved"] += 1
        log("  saved meetings/%s (%d entries)" % (fname, len(entries)))
    teams.back_to_chat()
