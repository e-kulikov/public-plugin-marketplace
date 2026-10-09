---
name: backup-teams-chat
description: Back up a Microsoft Teams chat to a local folder - messages (optionally within a date range), attached files and meeting transcripts (including transcripts that can only be viewed on the web page, not downloaded) - as chat.jsonl + chat.md, attachments/ and meetings/.
when_to_use: Use when the user asks to save, export, archive, download or parse a Teams chat/meeting chat, its messages for some dates, its files, or the transcript of a Teams call, or invokes this skill directly.
argument-hint: "<chat link|19:...@thread.v2> [...] [--from DATE] [--to DATE] [--out DIR] [--no-files] [--no-meetings] [--meetings-only]"
---

# backup-teams-chat

Back up Teams chats with the `teams-backup` command shipped in this plugin's `bin/` (on `PATH`;
otherwise `${CLAUDE_PLUGIN_ROOT}/bin/teams-backup`). You orchestrate it: prepare the browser,
let the user log in, run, report.

Never ask for or type the user's Microsoft password/MFA code. Never print access tokens (the page
keeps them in localStorage - do not read them). Never delete anything from the output folder.

## Result

```
<out>/<chat title>/
  chat.jsonl      stream of parsed messages, one JSON object per line; new runs APPEND (new ids, or a new
                  revision when a message was edited / got a file / reactions)
  chat.md         generated from chat.jsonl: chronological, one entry per message id, grouped by day
  chat.meta.json  chat id, title, time zone used for display
  attachments/    files attached in the chat: <YYYY-MM-DD_HHMM>__<original name>
  meetings/       transcripts: <YYYY-MM-DD_HHMM>_<chat title>.md  (start of the call = prefix)
```

jsonl fields: `id` (Teams message id = epoch ms), `ts` (UTC), `type` (`message`|`system`), `author`,
`author_id`, `text` (Markdown-ish), `attachments` (`name`, `file` or `error`), `reactions`, `edited`,
`rev`, `seen_at`. Times in `chat.md`, file prefixes and meeting names are in the time zone Teams displays.

## 1. Understand the request

Arguments: `$ARGUMENTS`

- chat(s): Teams chat links (any host: `teams.microsoft.com/l/chat/...`, `teams.cloud.microsoft/...`) or
  bare `19:...@thread.v2` ids. If none were given, ask for them (the user can copy the link via
  "Copy link" in the chat's menu). Channel posts are not supported - only chats and meeting chats.
- `--from` / `--to`: `YYYY-MM-DD` (whole day, in the chat's time zone) or ISO 8601; filter messages
  **and** meetings (by call start). Ask whether a date range is wanted if the user did not say.
- `--out DIR` (default: current directory), `--no-files`, `--no-meetings`, `--meetings-only`.

## 2. Prepare the browser

Microsoft sign-in is done by the user in a real Chrome with its own persistent profile, driven
through a DevTools port with `agent-browser`.

1. `teams-backup env` - `agent_browser` must be true (else: `npm i -g agent-browser && agent-browser install`).
2. `teams-backup browser status`. If not running, explain first: a separate Chrome window with a dedicated
   profile will open on Teams; under WSL Chrome starts on Windows and a port forward limited to the WSL
   network is used (may need admin approval once). Then `teams-backup browser start`.
3. `teams-backup session`. Exit code 3 = not logged in: tell the user to log in in that Chrome window
   (including MFA) themselves, **end your turn** and wait for their reply, then re-run `session`.
   The login stays in the profile, so later runs usually need no login.

## 3. Run

```
teams-backup run <chat> [<chat> ...] --out <dir> [--from D] [--to D] [--no-files] [--no-meetings|--meetings-only]
```

Run it with `run_in_background` for big chats and report progress from its output. It is safe to
rerun any time: messages are de-duplicated by id, existing files and meetings are not fetched again.
(To re-read a meeting transcript, delete its `.md` first - ask the user before deleting.)

The tool opens the chat, scrolls the history up to `--from`, downloads each file card through the
page's own *Download* action (Chrome saves into a staging folder, then the file is moved to
`attachments/`), then opens **Recap -> meeting selector -> Transcript** and reads the transcript from the
viewer page. The latter works also when "Download" is disabled for the user.

## 4. Report

Summarise from the JSON line printed per chat: messages added/revised, files saved/failed,
meetings saved and the `problems` list (no transcript recorded, no access, incomplete read). Point to
`chat.md` and `meetings/`. Offer `teams-backup browser stop` when the user is done. The folder
contains private conversations - suggest a private repo or encrypted storage if they plan to commit it.

## Troubleshooting

- `ERROR: no browser with a DevTools port found` - `teams-backup browser start`.
- The chat never opens / asks to install the desktop app: the tool clicks "Use the web app instead"
  itself; if Teams changed the page, open the chat manually in that Chrome window and rerun.
- "no Transcript tab" - the call had no transcript, or the user cannot see it in the Recap tab.
- "incomplete: N of M entries" - rerun after deleting that meeting file; Teams lazy-loads the list.
- UI strings (Recap, Transcript, Download) are matched in English; switch Teams to English if a non-English
  UI is not found. `teams-backup render <chat folder>` rebuilds `chat.md` offline from `chat.jsonl`.
- Not captured: images pasted inline into messages (shown as `[image]`), files in channel tabs,
  Loop components, voice/video recordings (they live in OneDrive/SharePoint).
