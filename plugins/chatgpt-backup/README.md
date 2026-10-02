# chatgpt-backup

Back up your ChatGPT conversations - including projects and attached files - to a local folder as
Markdown + JSON, and keep that folder up to date incrementally.

```
claude plugin marketplace add vercel-labs/agent-browser   # once: the dependency lives there
claude plugin marketplace add 'e-kulikov/public-plugin-marketplace#stable'
claude plugin install chatgpt-backup@e-kulikov-public-ai-marketplace
```

## Requirements

- **Python 3.8+** (standard library only)
- **Google Chrome** (the plugin starts its own window with a separate profile)
- **agent-browser** plugin (declared as a dependency, from the `agent-browser` marketplace) **and its CLI**:
  `npm i -g agent-browser && agent-browser install`
- WSL users: Windows interop (`powershell.exe`) must work; `gsudo` is used when available to create the
  port forward (otherwise Windows shows a UAC prompt).

## Usage

```
/chatgpt-backup:backup-chatgpt-conversation                       # asks what to back up
/chatgpt-backup:backup-chatgpt-conversation <id|link> [<id|link> ...]
/chatgpt-backup:backup-chatgpt-conversation --from 2026-01-01 --to 2026-03-31
/chatgpt-backup:backup-chatgpt-conversation --all
/chatgpt-backup:backup-chatgpt-conversation --missing             # only new + continued conversations
```

| Flag | Meaning |
|---|---|
| `<id>...` | conversation ids or `chatgpt.com/c/<id>` links, space- or comma-separated. Excludes `--from/--to`, `--all`, `--missing` |
| `--from`, `--to` | last-update window (`YYYY-MM-DD` = whole day, or ISO 8601; UTC). Either may be given alone |
| `--all` | every conversation (active, archived, and those inside projects) |
| `--missing` | only conversations that are not in `manifest.json` yet or were updated after the backup. May be combined with `--from/--to` |
| `--out DIR` | backup folder (default: current directory); `manifest.json` is read from there |
| `--dry-run` | print the plan and stop |
| `--no-files` | skip images and attachments |
| `--allow-mass-delete` | accept a large number of "deleted on the server" findings (normally treated as an incomplete list) |

Without a mode the skill asks (specific ids / time range / everything, and - when the folder already has a
`manifest.json` - *only what is missing*). Before downloading it always shows a plan (what is new, updated,
moved, deleted, time estimate) and asks for permission.

The same engine is available as a command: `chatgpt-backup run --missing --out ~/backups/chatgpt --yes`
(see `chatgpt-backup --help`; also `browser start|status|stop`, `session`, `rebuild`, `migrate`).

## What you get

```
<out>/
  manifest.json            index + checkpoint (schema 2)
  index.md                 table of contents per project
  unavailable.md           files that could not be downloaded, with reasons
  no-project/<date>_<title>__<id>/
      conversation.md      readable transcript (active branch, voice transcripts, links to files)
      conversation.json    the raw API payload, all branches, nothing dropped
      artifacts/           images and attachments (<file id>__<name>)
  projects/<project>/
      project.json, instructions.md
      <date>_<title>__<id>/ ...
```

## How updates work

- The server's conversation list is compared with `manifest.json` **per conversation** (not by a single date).
  New conversations and conversations continued after the backup are downloaded again; the rest is untouched.
- **Deleted on the server:** nothing is removed locally; the manifest entry is flagged `deleted_on_server`.
- **Archived:** flagged `archived` (and unflagged again when restored).
- **Moved into / between projects, or a project renamed:** the folder is moved (`git mv` when the backup is a
  git repository); previous locations are kept in the entry's `path_history`.
- A conversation is recorded in the manifest only after it was written completely, so an interrupted run
  continues where it stopped (`--missing`). A lock file prevents two runs on one folder.
- Old manifests (a bare JSON list) are upgraded automatically from each folder's `conversation.json`.

## How it talks to ChatGPT

ChatGPT blocks automated browsers at login, so the plugin starts a **real Chrome with a dedicated, persistent
profile** and a DevTools port (on WSL: Chrome on Windows, reached through a port forward bound to the WSL
gateway address). You log in once, by hand; the session stays in that profile.
Conversations are read by JavaScript running inside that tab (through `agent-browser eval`), so requests look
like the site's own. Files are saved by the browser itself into a staging folder (a small background process
keeps Chrome from asking "allow multiple downloads?") and then moved into the backup.

## Privacy and security

- The backup contains your private conversations. Keep it in a **private** repository or encrypted storage.
- The dedicated Chrome profile lives in `%LOCALAPPDATA%\chatgpt-backup\chrome-profile` (Windows/WSL),
  `~/.local/share/chatgpt-backup/chrome-profile` (Linux) or `~/Library/Application Support/chatgpt-backup/chrome-profile`
  (macOS) and **keeps your ChatGPT session on disk**. Delete the folder to log out.
- A DevTools port gives full control of that browser. It is bound to loopback; under WSL the forward is bound to the
  WSL gateway address only, and the firewall rule it creates (`chatgpt-backup CDP`) allows the local subnet only.
  Close the browser with `chatgpt-backup browser stop` when you are done.
- Passwords and access tokens are never stored or printed by the plugin.

## Limitations

- The server serves roughly **one conversation per minute** before throttling; large backups take hours.
  The command waits and retries on HTTP 429.
- ChatGPT's web API is **undocumented** and may change; the plugin fails loudly instead of guessing.
- Not obtainable: voice recordings (the transcript is kept), files attached to projects (metadata is kept),
  files already deleted on the server, page previews of documents. They are listed in `unavailable.md`.
- Temporary chats are not stored by ChatGPT and cannot be backed up.
- Times are shown in UTC.

## Development

```
bash plugins/chatgpt-backup/tests/run-tests.sh      # offline unit tests, no browser or network
```
