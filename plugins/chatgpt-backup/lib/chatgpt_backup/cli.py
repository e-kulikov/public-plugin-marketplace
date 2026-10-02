"""Command line: ``chatgpt-backup <command>``.

Commands
  env                 describe the detected environment (wsl/linux/macos/windows)
  browser start|status|stop
  session             is the browser logged in to ChatGPT?
  run                 plan + (confirm) + back up   [--dry-run to stop after the plan]
  rebuild             re-generate every conversation.md from the saved JSON (offline)
  migrate             upgrade an old manifest.json to schema 2 (offline)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import browser as br
from . import plan as planmod
from . import render, store
from .api import ApiError, ChatGPT
from .cdp import AgentBrowser, CdpError, cdp_alive, start_holder, stop_holder
from .run import Lock, LockError, Runner


def _err(msg):
    print("error: " + msg, file=sys.stderr)


def build_parser():
    p = argparse.ArgumentParser(prog="chatgpt-backup", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("env", help="describe the environment")

    b = sub.add_parser("browser", help="manage the dedicated Chrome")
    b.add_argument("action", choices=["start", "status", "stop"])
    b.add_argument("--port", type=int, default=br.DEFAULT_PORT)
    b.add_argument("--proxy-port", type=int, default=br.DEFAULT_PROXY_PORT)

    s = sub.add_parser("session", help="check the ChatGPT login")
    s.add_argument("--cdp")
    s.add_argument("--port", type=int, default=br.DEFAULT_PORT)
    s.add_argument("--proxy-port", type=int, default=br.DEFAULT_PROXY_PORT)

    r = sub.add_parser("run", help="back up conversations")
    r.add_argument("ids", nargs="*", help="conversation ids or chatgpt.com links (space or comma separated)")
    r.add_argument("--from", dest="time_from", help="only conversations updated at/after this time (YYYY-MM-DD or ISO 8601, UTC)")
    r.add_argument("--to", dest="time_to", help="only conversations updated at/before this time")
    r.add_argument("--all", action="store_true", help="every conversation")
    r.add_argument("--missing", action="store_true", help="only new and updated conversations (needs/uses manifest.json)")
    r.add_argument("--out", default=".", help="backup folder (default: current directory)")
    r.add_argument("--dry-run", action="store_true", help="show the plan and stop")
    r.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    r.add_argument("--no-files", action="store_true", help="skip images/attachments")
    r.add_argument("--allow-mass-delete", action="store_true",
                   help="flag a large number of server-side deletions (normally suspected to be an incomplete list)")
    r.add_argument("--cdp", help="DevTools URL of an already running Chrome")
    r.add_argument("--port", type=int, default=br.DEFAULT_PORT)
    r.add_argument("--proxy-port", type=int, default=br.DEFAULT_PROXY_PORT)
    r.add_argument("--json", action="store_true", help="machine-readable plan/summary")

    for name in ("rebuild", "migrate"):
        x = sub.add_parser(name)
        x.add_argument("--out", default=".")
    return p


# ---------------------------------------------------------------------------

def _resolve_cdp(args):
    if getattr(args, "cdp", None):
        return args.cdp
    env = br.detect_env()
    url = br.find_live_cdp(env, args.port, args.proxy_port)
    if not url:
        raise CdpError("no browser with a DevTools port found. Run: chatgpt-backup browser start")
    return url


def cmd_env(_args):
    env = br.detect_env()
    out = {"env": env}
    try:
        out["dirs"] = br.app_dirs(env)
        out["chrome"] = br.find_chrome(env)
    except br.BrowserSetupError as e:
        out["error"] = str(e)
    out["agent_browser"] = bool(__import__("shutil").which("agent-browser"))
    print(json.dumps(out, indent=1))
    return 0


def cmd_browser(args):
    if args.action == "start":
        info = br.ensure_browser(args.port, args.proxy_port)
        print(json.dumps({"cdp_url": info["cdp_url"], "env": info["env"], "started": info["started"],
                          "profile": info["dirs"]["profile_host"]}, indent=1))
        return 0
    if args.action == "status":
        env = br.detect_env()
        url = br.find_live_cdp(env, args.port, args.proxy_port)
        print(json.dumps({"running": bool(url), "cdp_url": url}))
        return 0 if url else 1
    br.stop_browser(args.port, args.proxy_port)
    return 0


def cmd_session(args):
    api = ChatGPT(AgentBrowser(_resolve_cdp(args)))
    s = api.session()
    print(json.dumps(s))
    return 0 if s.get("logged_in") else 3


def _confirm(args, summary):
    if args.yes:
        return True
    if sys.stdin.isatty():
        return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")
    _err("refusing to start without confirmation (pass --yes after reviewing the plan)")
    return False


def cmd_run(args):
    try:
        ids = planmod.parse_ids(args.ids) if args.ids else []
        t_from = planmod.parse_time(args.time_from) if args.time_from else None
        t_to = planmod.parse_time(args.time_to, end=True) if args.time_to else None
        mode = planmod.validate_mode(ids, t_from, t_to, args.all, args.missing)
    except planmod.ArgError as e:
        _err(str(e))
        return 2
    if mode is None:
        _err("choose what to back up: conversation ids, --from/--to, --all or --missing")
        return 2

    out = os.path.abspath(args.out)
    manifest = store.Manifest.load(out)
    migrated = store.migrate(out, manifest) if manifest.conversations else 0
    if mode == "missing" and not manifest.conversations:
        print("note: no manifest.json in %s - every conversation counts as new" % out)

    cdp_url = _resolve_cdp(args)
    api = ChatGPT(AgentBrowser(cdp_url), log=print)
    sess = api.session()
    if not sess.get("logged_in"):
        _err("the browser is not logged in to ChatGPT. Log in in the Chrome window and retry.")
        return 3

    print("Reading your conversation list ...")
    listing = api.list_conversations()
    pids = sorted({it["project_id"] for it in listing if it.get("project_id")})
    projects = {}
    for pid in pids:
        try:
            projects[pid] = api.project(pid)
        except ApiError as e:
            print("warning: project %s unreadable (%s); using its id as the folder name" % (pid, e))
            projects[pid] = {"id": pid, "name": pid, "raw": {}}
    names = {pid: p["name"] for pid, p in projects.items()}

    plan = planmod.build_plan(mode, listing, manifest.conversations, ids=ids, time_from=t_from, time_to=t_to,
                              project_names=names, want_files=not args.no_files,
                              allow_mass_delete=args.allow_mass_delete)
    summary = planmod.summarize(plan)
    if migrated:
        print("(upgraded %d legacy manifest entries in memory)" % migrated)
    print(planmod.format_report(summary, mode, out))
    if args.json:
        print(json.dumps({"mode": mode, "summary": summary}))
    if args.dry_run:
        return 0
    if not plan["fetch"] and not plan["sync"] and not plan["deleted"]:
        print("Nothing to do.")
        return 0
    if not _confirm(args, summary):
        print("Cancelled.")
        return 1

    holder_stop = None
    downloads = None
    try:
        with Lock(out):
            if plan["fetch"] and not args.no_files:
                dirs = br.app_dirs()
                downloads = dirs["downloads_here"]
                os.makedirs(downloads, exist_ok=True)
                _pid, holder_stop = start_holder(cdp_url, dirs["downloads_host"], dirs["state"])
                print("Download holder started (files go to %s, then into the backup)." % dirs["downloads_host"])
            runner = Runner(out, api, projects, downloads_dir=downloads, want_files=not args.no_files,
                            log=print, manifest=manifest)
            by_id = {it["id"]: it for it in listing}
            failures = runner.execute(plan, by_id)
    except LockError as e:
        _err(str(e))
        return 4
    finally:
        if holder_stop:
            stop_holder(holder_stop)

    done = len(plan["fetch"]) - sum(1 for f in failures if f[0] in plan["fetch"])
    print("\nDone: %d/%d conversations backed up, %d metadata updates, %d marked deleted."
          % (done, len(plan["fetch"]), len(plan["sync"]), len(plan["deleted"])))
    if failures:
        print("Problems (%d):" % len(failures))
        for cid, title, msg in failures:
            print("  - %s %r: %s" % (cid, title, msg))
    if args.json:
        print(json.dumps({"done": done, "failures": [{"id": c, "title": t, "message": m} for c, t, m in failures]}))
    return 1 if failures else 0


def cmd_rebuild(args):
    out = os.path.abspath(args.out)
    manifest = store.Manifest.load(out)
    if not manifest.conversations:
        _err("no manifest.json in %s" % out)
        return 2
    store.migrate(out, manifest)
    runner = Runner(out, None, {}, manifest=manifest)
    bad = []
    for e in manifest.conversations:
        try:
            conv = runner.rerender(e)
            if e["messages"] != render.expected_message_count(conv):
                bad.append(e["id"])
        except (OSError, ValueError, KeyError) as ex:
            bad.append("%s (%s)" % (e["id"], ex))
    manifest.save()
    store.write_top_level(out, manifest)
    print("Re-rendered %d conversations; %d problems%s" % (len(manifest.conversations), len(bad),
                                                          ": " + ", ".join(bad) if bad else ""))
    return 1 if bad else 0


def cmd_migrate(args):
    out = os.path.abspath(args.out)
    manifest = store.Manifest.load(out)
    n = store.migrate(out, manifest)
    manifest.save()
    print("Manifest is schema %d; %d entries upgraded." % (store.SCHEMA, n))
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return {"env": cmd_env, "browser": cmd_browser, "session": cmd_session,
                "run": cmd_run, "rebuild": cmd_rebuild, "migrate": cmd_migrate}[args.cmd](args)
    except (CdpError, br.BrowserSetupError, ApiError) as e:
        _err(str(e))
        return 1
    except KeyboardInterrupt:
        _err("interrupted; progress is saved in manifest.json, rerun with --missing to continue")
        return 130
