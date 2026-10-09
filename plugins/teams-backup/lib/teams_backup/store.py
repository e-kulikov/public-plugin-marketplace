"""On-disk layout and rendering. No browser, no network.

<out>/<chat title>/
    chat.meta.json   chat id, title, time zone used for display
    chat.jsonl       append-only stream of parsed messages (one JSON object per line)
    chat.md          chronological, de-duplicated view generated from chat.jsonl
    attachments/     files attached in the chat (<date>_<time>__<original name>)
    meetings/        transcripts: <YYYY-MM-DD_HHMM>_<chat title>.md
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name, default="chat", limit=120):
    s = _BAD.sub("_", (name or "").replace(" ", " ")).strip(" .")
    s = re.sub(r"\s+", " ", s)
    return (s[:limit].rstrip(" .") or default)


_ALIASES = {"America/Buenos_Aires": "America/Argentina/Buenos_Aires", "Europe/Kiev": "Europe/Kyiv", "Asia/Calcutta": "Asia/Kolkata",
            "Asia/Katmandu": "Asia/Kathmandu", "Asia/Saigon": "Asia/Ho_Chi_Minh", "Asia/Rangoon": "Asia/Yangon"}


def get_tz(name, offset_min=None):
    """IANA zone when this machine knows it (also under its old alias), else the fixed offset the browser reported."""
    if name and ZoneInfo:
        for n in (name, _ALIASES.get(name)):
            if n:
                try:
                    return ZoneInfo(n)
                except Exception:
                    pass
    if offset_min is not None:
        return dt.timezone(dt.timedelta(minutes=int(offset_min)))
    return dt.timezone.utc


def ms_to_iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(ms) % 1000)


def iso_to_dt(iso):
    return dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)


def parse_when(text, tz, end=False):
    """``YYYY-MM-DD`` (whole day in ``tz``; ``end`` -> exclusive upper bound) or ISO 8601 -> aware UTC datetime."""
    if not text:
        return None
    t = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        d = dt.datetime.strptime(t, "%Y-%m-%d").replace(tzinfo=tz)
        if end:
            d += dt.timedelta(days=1)
        return d.astimezone(dt.timezone.utc)
    d = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz)
    return d.astimezone(dt.timezone.utc)


# ---------------------------------------------------------------------------
# chat.jsonl
# ---------------------------------------------------------------------------

def _sig(rec):
    """What counts as a change worth a new revision."""
    return json.dumps([rec.get("text"), [(a.get("name"), bool(a.get("file"))) for a in rec.get("attachments", [])],
                       rec.get("reactions"), rec.get("edited")], ensure_ascii=False, sort_keys=True)


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a torn last line from an interrupted run
    return out


def latest_by_id(records):
    """Last revision of every message id."""
    cur = {}
    for r in records:
        cur[r["id"]] = r
    return cur


def append_new(path, records, now_iso):
    """Append records that are new or changed; return (added, revised, unchanged)."""
    cur = latest_by_id(read_jsonl(path))
    added = revised = same = 0
    lines = []
    for r in records:
        old = cur.get(r["id"])
        if old is None:
            r = dict(r, rev=1, seen_at=now_iso)
            added += 1
        elif _sig(old) != _sig(r):
            r = dict(r, rev=old.get("rev", 1) + 1, seen_at=now_iso)
            revised += 1
        else:
            same += 1
            continue
        cur[r["id"]] = r
        lines.append(json.dumps(r, ensure_ascii=False, sort_keys=True))
    if lines:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return added, revised, same


# ---------------------------------------------------------------------------
# chat.md
# ---------------------------------------------------------------------------

def render_md(records, meta):
    tz = get_tz(meta.get("tz"), meta.get("tz_offset"))
    items = sorted(latest_by_id(records).values(), key=lambda r: (r["ts"], r["id"]))
    out = ["# %s" % (meta.get("title") or "Teams chat"), ""]
    if meta.get("chat_id"):
        out.append("- Chat: `%s`" % meta["chat_id"])
    if items:
        first, last = iso_to_dt(items[0]["ts"]).astimezone(tz), iso_to_dt(items[-1]["ts"]).astimezone(tz)
        out.append("- Messages: %d (%s - %s)" % (len(items), first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")))
    out.append("- Times: %s" % (meta.get("tz") or "UTC"))
    day = None
    for r in items:
        t = iso_to_dt(r["ts"]).astimezone(tz)
        if t.date() != day:
            day = t.date()
            out += ["", "## %s (%s)" % (day.isoformat(), t.strftime("%A"))]
        hm = t.strftime("%H:%M")
        if r.get("type") == "system":
            out += ["", "_%s - %s_" % (hm, (r.get("text") or "").replace("\n", " "))]
            continue
        who = r.get("author") or "?"
        out += ["", "**%s %s**%s" % (hm, who, " _(edited)_" if r.get("edited") else "")]
        if r.get("text"):
            out.append(r["text"])
        for a in r.get("attachments", []):
            out.append("- Attachment: [%s](%s)" % (a["name"], a["file"].replace(" ", "%20")) if a.get("file")
                       else "- Attachment (not downloaded): %s%s" % (a["name"], " - " + a["error"] if a.get("error") else ""))
        if r.get("reactions"):
            out.append("_%s_" % "; ".join(r["reactions"]))
    return "\n".join(out).rstrip() + "\n"


def write_md(chat_dir, meta):
    recs = read_jsonl(os.path.join(chat_dir, "chat.jsonl"))
    with open(os.path.join(chat_dir, "chat.md"), "w", encoding="utf-8") as f:
        f.write(render_md(recs, meta))
    return len(latest_by_id(recs))


def read_meta(chat_dir):
    p = os.path.join(chat_dir, "chat.meta.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_meta(chat_dir, meta):
    os.makedirs(chat_dir, exist_ok=True)
    with open(os.path.join(chat_dir, "chat.meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# meetings
# ---------------------------------------------------------------------------

_LABEL = re.compile(r"^\s*(?:[A-Za-z]+,\s*)?([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)\s*$")


def parse_meeting_label(label, tz):
    """'Friday, October 9, 2026 9:30 AM - 10:00 AM' -> (start, end) aware datetimes in ``tz`` or None."""
    m = _LABEL.match(re.sub(r"\s+", " ", (label or "").replace(" ", " ")).replace(" -  ", " - "))
    if not m:
        return None
    mon, day, year, t1, t2 = m.groups()
    try:
        d = dt.datetime.strptime("%s %s %s" % (mon, day, year), "%B %d %Y")
        s = dt.datetime.strptime(t1.replace(" ", ""), "%I:%M%p").time()
        e = dt.datetime.strptime(t2.replace(" ", ""), "%I:%M%p").time()
    except ValueError:
        return None
    start = dt.datetime.combine(d.date(), s, tz)
    end = dt.datetime.combine(d.date(), e, tz)
    if end < start:
        end += dt.timedelta(days=1)
    return start, end


_DUR = re.compile(r"^(.*?)\s*(?:(\d+)\s+hours?)?\s*(?:(\d+)\s+minutes?)?\s*(?:(\d+)\s+seconds?)?\s*$")


def split_entry_label(label):
    """'Andrei Salanoi 1 hour 2 minutes 3 seconds' -> ('Andrei Salanoi', 3723). Blank label -> (None, None)."""
    label = (label or "").replace(" ", " ").strip()
    if not label:
        return None, None
    m = _DUR.match(label)
    h, mi, s = (int(x) if x else None for x in m.groups()[1:])
    if h is None and mi is None and s is None:
        return label, None
    return m.group(1).strip() or None, (h or 0) * 3600 + (mi or 0) * 60 + (s or 0)


def fmt_offset(sec):
    sec = int(sec)
    return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60) if sec >= 3600 else "%d:%02d" % (sec // 60, sec % 60)


def meeting_filename(start, title):
    return "%s_%s.md" % (start.strftime("%Y-%m-%d_%H%M"), sanitize(title, "meeting", 100))


def render_transcript(entries, chat_title, label, start, tz_name):
    out = ["# %s" % chat_title, "",
           "- Meeting: %s" % re.sub(r"\s+", " ", label or ""),
           "- Start: %s (%s)" % (start.strftime("%Y-%m-%d %H:%M") if start else "?", tz_name or "UTC"),
           "- Entries: %d" % len(entries), ""]
    prev = object()
    for e in entries:
        who, off = split_entry_label(e.get("label"))
        text = (e.get("text") or "").strip()
        if who is None and off is None:      # "<name> started/stopped transcription"
            out += ["_%s_" % text, ""]
            prev = object()
            continue
        if who != prev:
            out += ["**%s**" % (who or "?"), ""]
            prev = who
        out += ["`%s` %s" % (fmt_offset(off) if off is not None else "?", text), ""]
    return "\n".join(out).rstrip() + "\n"
