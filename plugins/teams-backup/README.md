# teams-backup

Back up a Microsoft Teams chat to a local folder: messages (optionally for a date range),
attached files and **meeting transcripts - also those you cannot download as a file**, only view on a web page.

```
claude plugin marketplace add vercel-labs/agent-browser   # once: the dependency lives there
claude plugin marketplace add 'e-kulikov/public-plugin-marketplace#stable'
claude plugin install teams-backup@e-kulikov-public-ai-marketplace
```

## Requirements

- **Python 3.9+** (standard library only)
- **Google Chrome**; the plugin starts its own window with a separate profile
- **agent-browser** plugin (dependency) **and its CLI**: `npm i -g agent-browser && agent-browser install`
- WSL: Windows interop (`powershell.exe`); the DevTools port is forwarded to WSL like in `chatgpt-backup`
- Teams web UI in **English** (tab/button names are matched by text)

## Usage

```
/teams-backup:backup-teams-chat <chat link> [--from 2026-10-01] [--to 2026-10-09] [--out DIR]
```

Command line: `teams-backup browser start`, `teams-backup session`, then
`teams-backup run <link|19:...@thread.v2> ... --out DIR [--from D] [--to D] [--no-files] [--no-meetings] [--meetings-only]`;
`teams-backup render DIR` rebuilds `chat.md` offline.

## What you get

```
<out>/<chat title>/
  chat.jsonl      parsed messages; every run appends new ones (edits become a new revision)
  chat.md         chronological, de-duplicated, generated from chat.jsonl
  chat.meta.json
  attachments/    <date>_<time>__<file name>
  meetings/       <YYYY-MM-DD_HHMM>_<chat title>.md   (call start = prefix)
```

## How it works

1. A real Chrome with a dedicated profile and a DevTools port is started on the Teams page; you sign in by hand
   (the session stays in that profile). Teams blocks nothing here because the browser is not automation-flagged.
2. `agent-browser` runs JavaScript in the Teams tab: the chat is opened by its link (the "get the desktop app" page is
   skipped with *Use the web app instead*), the history is scrolled up to `--from`, messages are read from the page
   (Teams message id = creation time in ms, author, text as Markdown, file cards, reactions).
3. Each file card is downloaded with the page's own *Download* action; Chrome saves it to a staging folder.
4. Transcripts: *Recap* -> meeting selector -> *Transcript*. The viewer is a cross-origin iframe (SharePoint) with a
   virtualised list, so the plugin attaches to that iframe's DevTools target and scrolls through all entries
   (it checks the count against the list's `aria-setsize`).

## Privacy and limitations

- The output holds private conversations; the browser profile (`%LOCALAPPDATA%\teams-backup\chrome-profile` on
  Windows/WSL, `~/.local/share/teams-backup/chrome-profile` on Linux) keeps your Microsoft session. Delete it to log out.
- The DevTools port gives full control of that Chrome; it is bound to loopback / the WSL gateway only.
  `teams-backup browser stop` closes it. No password or token is read, stored or printed.
- Teams' web UI is undocumented and changes; selectors were taken from the live DOM and the tool fails loudly
  instead of guessing. Messages are read from what the page renders, so very old history depends on lazy loading.
- Not captured: inline pasted images (`[image]`), channel posts/tabs, Loop components, recordings.
- A transcript is only available if one was recorded and you can see it in the Recap tab.

## Development

```
bash plugins/teams-backup/tests/run-tests.sh   # offline unit tests
```
