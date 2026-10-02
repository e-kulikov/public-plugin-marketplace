---
name: backup-chatgpt-conversation
description: Back up ChatGPT conversations (with projects and attached files) to a local folder as Markdown + JSON, either everything, a list of conversation ids, a date range, or only what is new or changed since the last backup.
when_to_use: Use when the user asks to back up, export, archive, download or sync their ChatGPT conversations/chats/projects, or invokes this skill directly.
argument-hint: "[<id|link> ...] [--from DATE] [--to DATE] [--all] [--missing] [--out DIR] [--dry-run] [--no-files]"
---

# backup-chatgpt-conversation

Back up the user's ChatGPT conversations into a folder. The work is done by the
`chatgpt-backup` command shipped in this plugin's `bin/` (on `PATH`; otherwise call
`${CLAUDE_PLUGIN_ROOT}/bin/chatgpt-backup`). You orchestrate it: ask the right
questions, prepare the browser, show the plan, get permission, run, report.

Never ask for or type the user's ChatGPT password. Never print or save access tokens.
Never delete anything from a backup folder.

## 1. Understand the request

Arguments given: `$ARGUMENTS`

Modes (they map 1:1 onto `chatgpt-backup run` flags):

| User wants | Flags |
|---|---|
| specific conversations | one or more ids or `chatgpt.com/c/...` links, space- or comma-separated |
| a time window | `--from DATE` and/or `--to DATE` (`YYYY-MM-DD` or ISO 8601, UTC; filters by last update) |
| everything | `--all` |
| only what is new or changed since the last backup | `--missing` (may be combined with `--from/--to`) |

Rules: ids exclude `--from/--to`, `--all` and `--missing`; `--all` excludes the rest.
Other flags: `--out DIR` (backup folder, default = current directory), `--dry-run`,
`--no-files` (skip images/attachments).

### No mode given

If the arguments contain no ids, no `--from/--to`, no `--all` and no `--missing`, ask.
First resolve the backup folder (`--out`, else the current directory) and check whether
`<folder>/manifest.json` exists. Then use `AskUserQuestion`:

- "How should I choose what to back up?" with options
  1. **Specific conversations** - then ask for the ids/links (free text, spaces or commas).
  2. **A time range** - then ask for `--from` and/or `--to` (either may be left empty).
  3. **All conversations**
  4. **Only what is missing** - offer this **only if `manifest.json` exists**: new conversations
     plus those continued after the last backup (the manifest's freshest conversation is the
     reference point; mention its date).

Also confirm the folder if it is not obvious.

## 2. Prepare the browser

ChatGPT blocks automated browsers at login, so the plugin drives a real Chrome with its own
persistent profile (separate from the user's everyday profile) through a DevTools port.

1. `chatgpt-backup env` - shows the environment (`wsl`, `linux`, `macos`, `windows`), the Chrome
   it found and whether the `agent-browser` CLI is installed. If `agent_browser` is false, stop and
   tell the user to run `npm i -g agent-browser && agent-browser install`.
2. `chatgpt-backup browser status`. If it is not running, **explain before acting**, in plain words:
   - what you are about to do: open a separate Chrome window with a dedicated profile for the backup;
   - why: ChatGPT must be logged in, and the backup reads the conversations through that window;
   - under WSL: Chrome is started on Windows, and a local port forward (limited to the WSL network)
     is created so Linux can talk to it; creating it may ask for administrator approval on Windows.
   Then run `chatgpt-backup browser start`. If it reports that administrator rights are missing, show
   the user the exact commands it printed and wait for them to run them.
3. `chatgpt-backup session`. If `logged_in` is `true`, continue. If not (exit code 3):
   - tell the user a Chrome window is open on chatgpt.com and that **they must log in there
     themselves** (you never enter credentials), including any CAPTCHA;
   - the login is remembered in this profile, so later backups usually need no login;
   - **end your turn** and wait until they reply that it works, then run `chatgpt-backup session` again.

## 3. Show the plan and ask for permission

Always run the plan first (it only reads; it writes nothing):

```
chatgpt-backup run <mode flags> --out <folder> --dry-run
```

Show the report to the user: conversations to download (new / updated / requested), metadata-only
changes (moved between projects, archived/unarchived, restored), conversations deleted on the server
(only flagged, never removed locally), what is already up to date and the time estimate.
The server allows roughly one conversation per minute, so large backups take hours - say so.

If the report contains a `WARNING ... NOT flagged` line (many conversations apparently vanished),
this usually means an incomplete server list, not real deletions: tell the user, do **not** add
`--allow-mass-delete` unless they explicitly ask.

If `--dry-run` was requested, stop here. Otherwise ask for confirmation with `AskUserQuestion`
(proceed / change the selection / cancel). If there is nothing to do, say so and stop.

## 4. Run

Run in the background so the session stays usable:

```
chatgpt-backup run <mode flags> --out <folder> --yes
```

Use `run_in_background`, then read its output periodically and tell the user what is happening
(`[n/total]` lines, one per conversation). It is safe to interrupt and rerun: the manifest is
updated after each finished conversation, so `--missing` continues where it stopped.
Only one backup can run per folder (a lock file prevents overlaps).

If the output says the browser is not logged in, or Chrome is not reachable, go back to step 2.

## 5. Report

When it finishes, summarise: how many conversations were saved, metadata updates, conversations
flagged as deleted, and every problem listed under "Problems". Mention `unavailable.md` for files
that could not be downloaded (deleted on the server, audio recordings, project files). Offer, but
do not do unprompted: committing the folder to git (it contains personal conversations - use a
private repository), and `chatgpt-backup browser stop` to close the backup Chrome.

## Behaviour of updates (for your explanations)

- Updated conversations (continued after the backup) are re-downloaded and rewritten; new ones are added.
- Deleted on the server: nothing is removed; the manifest entry gets `deleted_on_server: true`.
- Moved to the archive: marked `archived`. Moved to or between projects: the folder is moved
  (`git mv` in a git repository) and the previous location is kept in `path_history`.
- `chatgpt-backup rebuild --out <folder>` regenerates every `conversation.md` from the saved JSON
  without network access (useful after a plugin update). `chatgpt-backup migrate` upgrades an old
  manifest.

## Troubleshooting

- `no browser with a DevTools port found` - run `chatgpt-backup browser start`.
- `agent-browser ... failed` - make sure the `agent-browser` CLI works (`agent-browser --help`);
  if `AGENT_BROWSER_CA_CERT` is set the plugin unsets it for its own calls.
- Files stay "error" - the download holder could not allow downloads; rerun with `--missing`
  (incomplete conversations are retried) or ask the user to click "Allow" if Chrome shows a prompt.
- HTTP 429 messages (`server busy`) are normal: the command waits and retries.
