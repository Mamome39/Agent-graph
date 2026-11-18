# What Session Graph reads, writes and sends

**Sends:** nothing. There is no telemetry, no account, no update check unless you turn `release_check` on (one
request to Homebrew's public API per day for Claude Code's latest version number). The helper makes no other
network requests; the agents you run make theirs.

**Listens:** `127.0.0.1` only (not reachable from other machines). Requests need the per-install access key and a
matching Host/Origin header.

**Reads (Claude Code only, read-only):** Claude Code's `sessions/*.json` (never the `.key` files) and the session
transcripts under `projects/`, to show activity. The folder is `$CLAUDE_CONFIG_DIR` or `~/.claude`, or `claude_dir`.

**Runs:** the agent commands you configure, in the folders you choose, with your permissions. The "open in editor",
"navigate to terminal" and "choose folder" buttons run `code`, `open`/`xdg-open`, `osascript`/`zenity` and a terminal
program, only when you press them.

**Writes**, all inside the data folder (`session-graph where`):

| File | What |
| --- | --- |
| `config.json` | your settings |
| `state.json` | which sessions are tracked, names, closed marks, parent links, defaults |
| `stats.json` | counts for the activity panel (prompts per hour/day, agents per day), no text |
| `transcripts/` | conversation transcripts of ACP-agent sessions (they contain your prompts and the agents' replies) |
| `attachments/` | files you attached that can't be sent inline |
| `.token`, `server.pid`, `server.log`, `probe/` | access key (mode 600), process id, log, scratch folder for the usage check |

Deleting the data folder removes everything the board knows. Deleting a session's transcript (turn on `cleanup`, or
delete the file) doesn't affect the agent's own copy, except for Claude Code sessions, where `cleanup` also removes
Claude Code's own transcript, `session-env`, `file-history` and related files for sessions you closed.

**Sharing the code:** the repository contains no data folder. Run `python3 tests/scrub_check.py` before you hand a copy
to anyone; it looks for your user name, home path, host name and any extra terms you give it.
