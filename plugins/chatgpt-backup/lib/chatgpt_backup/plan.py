"""Decide what to back up: argument parsing, filters and the manifest diff.

Pure functions only (no network, no disk) so the whole decision logic is unit-testable.
"""
from __future__ import annotations

import datetime
import re

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# The server answers roughly one conversation per minute before throttling.
SECONDS_PER_CONVERSATION = 60

# list update_time is never later than the conversation's own update_time for an
# unchanged conversation; allow this much slack when only the latter is known (schema 1).
LEGACY_TOLERANCE = 2.0
LIST_TOLERANCE = 0.5


class ArgError(ValueError):
    pass


def parse_ids(tokens):
    """Conversation ids from tokens split by spaces/commas; chatgpt.com URLs are accepted."""
    out, seen = [], set()
    for tok in tokens:
        for part in re.split(r"[,\s]+", tok.strip()):
            if not part:
                continue
            m = UUID_RE.search(part)
            if not m:
                raise ArgError("not a conversation id or chatgpt.com link: %r" % part)
            cid = m.group(0).lower()
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out


def parse_time(value, end=False):
    """``YYYY-MM-DD`` | ISO datetime (``Z`` or offset, UTC when omitted) | epoch -> epoch seconds.

    A bare date means the whole day: start of day for ``--from``, end of day for ``--to``.
    """
    v = value.strip()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", v):
        return float(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        d = datetime.datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        return d.timestamp() + (86400 - 0.001 if end else 0)
    try:
        d = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ArgError("cannot parse time %r (use YYYY-MM-DD or ISO 8601)" % value)
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def iso_to_epoch(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def validate_mode(ids, time_from, time_to, all_, missing):
    """Mode rules: ids excludes --from/--to/--all/--missing; --missing may add --from/--to."""
    if ids and (time_from is not None or time_to is not None or all_ or missing):
        raise ArgError("conversation ids cannot be combined with --from/--to, --all or --missing")
    if all_ and (time_from is not None or time_to is not None or missing):
        raise ArgError("--all cannot be combined with --from/--to or --missing")
    if time_from is not None and time_to is not None and time_from > time_to:
        raise ArgError("--from is later than --to")
    if not (ids or all_ or missing or time_from is not None or time_to is not None):
        return None
    if ids:
        return "ids"
    if missing:
        return "missing"
    if all_:
        return "all"
    return "range"


def entry_update_state(entry, listing_item):
    """True when the server copy is newer than the backed-up one."""
    lu = listing_item["update_time"]
    stored_list = entry.get("list_update_time")
    if stored_list is not None:
        return lu > stored_list + LIST_TOLERANCE
    detail = entry.get("update_time")
    if detail is None:
        return True
    return lu > detail + LEGACY_TOLERANCE


def build_plan(mode, listing, manifest_entries, ids=None, time_from=None, time_to=None,
               project_names=None, want_files=True, allow_mass_delete=False):
    """Compute the plan.

    listing: ``[{id,title,create_time,update_time,archived,project_id}]`` (epoch floats)
    manifest_entries: list of manifest conversation dicts
    Returns a dict with ``fetch`` (id -> reason), ``sync`` (moves / flag changes for
    conversations that are not refetched), ``deleted``, ``unchanged``, ``unknown_ids``.
    """
    project_names = project_names or {}
    by_id = {e["id"]: e for e in manifest_entries}
    srv = {it["id"]: it for it in listing}
    fetch, unknown = {}, []

    def in_range(it):
        if time_from is not None and it["update_time"] < time_from:
            return False
        if time_to is not None and it["update_time"] > time_to:
            return False
        return True

    if mode == "ids":
        for cid in ids:
            fetch[cid] = "requested"
            if cid not in srv:
                unknown.append(cid)  # not in the list (shared/temporary/deleted): still try
    else:
        for it in listing:
            cid = it["id"]
            e = by_id.get(cid)
            if mode == "all":
                fetch[cid] = "all"
            elif mode == "range":
                if in_range(it):
                    fetch[cid] = "in range"
            elif mode == "missing":
                if time_from is not None or time_to is not None:
                    if not in_range(it):
                        continue
                if e is None:
                    fetch[cid] = "new"
                elif entry_update_state(e, it):
                    fetch[cid] = "updated"
                elif want_files and any(f.get("status") in ("error", "skipped") for f in e.get("files") or []):
                    fetch[cid] = "incomplete"

    sync, deleted, unchanged = [], [], 0
    if mode != "ids":
        for e in manifest_entries:
            cid = e["id"]
            it = srv.get(cid)
            if it is None:
                if not e.get("deleted_on_server"):
                    deleted.append(cid)
                continue
            if e.get("deleted_on_server"):
                sync.append({"id": cid, "change": "restored"})
            if cid in fetch:
                continue
            changes = []
            if bool(e.get("archived")) != bool(it["archived"]):
                changes.append("archived" if it["archived"] else "unarchived")
            if (e.get("project_id") or None) != (it.get("project_id") or None) \
                    or (it.get("project_id") and e.get("project") != project_names.get(it["project_id"], e.get("project"))):
                changes.append("moved")
            if changes:
                sync.append({"id": cid, "change": "+".join(changes)})
            else:
                unchanged += 1
    suspicious = []
    if deleted and not allow_mass_delete and len(deleted) > max(5, 0.1 * len(manifest_entries)):
        # An incomplete server list would look exactly like this: do not flag anything.
        suspicious, deleted = deleted, []
    return {"mode": mode, "fetch": fetch, "sync": sync, "deleted": deleted,
            "deleted_suspicious": suspicious, "unchanged": unchanged, "unknown_ids": unknown}


def summarize(plan, files_estimate=None):
    reasons = {}
    for r in plan["fetch"].values():
        reasons[r] = reasons.get(r, 0) + 1
    n = len(plan["fetch"])
    seconds = n * SECONDS_PER_CONVERSATION
    return {
        "to_fetch": n,
        "by_reason": reasons,
        "sync_only": len(plan["sync"]),
        "marked_deleted": len(plan["deleted"]),
        "deleted_suspicious": len(plan.get("deleted_suspicious") or []),
        "unchanged": plan["unchanged"],
        "unknown_ids": plan["unknown_ids"],
        "estimate_minutes": round(seconds / 60.0),
    }


def format_report(summary, mode, out_dir):
    lines = ["Backup plan (%s) -> %s" % (mode, out_dir),
             "  conversations to download : %d%s" % (
                 summary["to_fetch"],
                 "  (" + ", ".join("%d %s" % (v, k) for k, v in sorted(summary["by_reason"].items())) + ")"
                 if summary["by_reason"] else ""),
             "  metadata-only changes      : %d (moved between projects / archived / restored)" % summary["sync_only"],
             "  deleted on the server      : %d (kept locally, flagged in the manifest)" % summary["marked_deleted"],
             "  left as they are           : %d (up to date, or outside the selection)" % summary["unchanged"]]
    if summary.get("deleted_suspicious"):
        lines.append("  WARNING: %d conversations are missing from the server list - too many to be real deletions, "
                     "so they are NOT flagged. Re-run with --allow-mass-delete if you are sure."
                     % summary["deleted_suspicious"])
    if summary["unknown_ids"]:
        lines.append("  ids not in your list       : %s (will still be tried)" % ", ".join(summary["unknown_ids"]))
    lines.append("  estimated time             : ~%d min (the server allows about one conversation "
                 "per minute; files add a little)" % summary["estimate_minutes"])
    return "\n".join(lines)
