"""teams-backup command line."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import browser, cdp, store
from .run import backup_chat
from .teams import NotLoggedIn, Teams, TeamsError


def _cdp_url(args):
    url = browser.find_live_cdp(browser.detect_env(), args.port, args.proxy_port)
    if not url:
        raise TeamsError("no browser with a DevTools port found - run `teams-backup browser start`")
    return url


def cmd_env(args):
    env = browser.detect_env()
    out = {"env": env, "agent_browser": bool(cdp.shutil.which("agent-browser"))}
    try:
        out["chrome"] = browser.find_chrome(env)
    except browser.BrowserSetupError as e:
        out["chrome"] = None
        out["chrome_error"] = str(e)
    out["dirs"] = browser.app_dirs(env)
    print(json.dumps(out, indent=2))
    return 0


def cmd_browser(args):
    if args.action == "start":
        info = browser.ensure_browser(args.port, args.proxy_port, url=args.url)
        print(json.dumps({"cdp_url": info["cdp_url"], "started": info["started"]}))
    elif args.action == "status":
        url = browser.find_live_cdp(browser.detect_env(), args.port, args.proxy_port)
        print(json.dumps({"running": bool(url), "cdp_url": url}))
        return 0 if url else 1
    else:
        browser.stop_browser(args.port, args.proxy_port)
    return 0


def cmd_session(args):
    st = Teams(_cdp_url(args)).check_session()
    print(json.dumps(st))
    return 0 if st["logged_in"] else 3


def cmd_run(args):
    teams = Teams(_cdp_url(args))
    st = teams.check_session()
    if not st["logged_in"]:
        print("Teams is not logged in (tab: %s). Log in in the opened Chrome window, then retry." % st["url"], file=sys.stderr)
        return 3
    out_root = os.path.abspath(args.out)
    dirs = browser.app_dirs()
    staging = dirs["downloads_here"]
    os.makedirs(staging, exist_ok=True)
    holder = None
    if args.files:
        _, holder = cdp.start_holder(teams.cdp_url, dirs["downloads_host"], dirs["state"])
    rc = 0
    try:
        for ref in args.chat:
            try:
                s = backup_chat(teams, ref, out_root, args.from_, args.to, files=args.files,
                                meetings=args.meetings, messages=args.messages, staging=staging)
                print(json.dumps(s, ensure_ascii=False))
            except NotLoggedIn as e:
                print("ERROR: %s" % e, file=sys.stderr)
                return 3
            except TeamsError as e:
                print("ERROR %s: %s" % (ref, e), file=sys.stderr)
                rc = 1
    finally:
        if holder:
            cdp.stop_holder(holder)
    return rc


def cmd_render(args):
    for d in args.dir:
        meta = store.read_meta(d)
        n = store.write_md(d, meta)
        print("%s: chat.md rebuilt (%d messages)" % (d, n))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="teams-backup", description="Back up Microsoft Teams chats, files and meeting transcripts.")
    p.add_argument("--port", type=int, default=browser.DEFAULT_PORT)
    p.add_argument("--proxy-port", type=int, default=browser.DEFAULT_PROXY_PORT)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("env").set_defaults(fn=cmd_env)
    b = sub.add_parser("browser")
    b.add_argument("action", choices=["start", "status", "stop"])
    b.add_argument("--url", default=None, help="page to open when Chrome is started (default: Teams)")
    b.set_defaults(fn=cmd_browser)
    sub.add_parser("session", help="exit code 3 when Teams is not logged in").set_defaults(fn=cmd_session)
    r = sub.add_parser("run", help="back up one or more chats")
    r.add_argument("chat", nargs="+", help="chat link or 19:...@thread.v2 id")
    r.add_argument("--out", default=".", help="output folder (a sub-folder per chat is created)")
    r.add_argument("--from", dest="from_", help="YYYY-MM-DD or ISO 8601 (chat's time zone when none given)")
    r.add_argument("--to", help="YYYY-MM-DD (whole day included) or ISO 8601")
    r.add_argument("--no-files", dest="files", action="store_false")
    r.add_argument("--no-meetings", dest="meetings", action="store_false")
    r.add_argument("--meetings-only", dest="messages", action="store_false")
    r.set_defaults(fn=cmd_run)
    g = sub.add_parser("render", help="rebuild chat.md from chat.jsonl (offline)")
    g.add_argument("dir", nargs="+")
    g.set_defaults(fn=cmd_render)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (TeamsError, browser.BrowserSetupError, cdp.CdpError) as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
