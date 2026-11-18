# Session Graph

A live board for your coding-agent sessions. One canvas shows every session as a node (folders → sessions →
sub-sessions → agents), whether it's working, idle or waiting for you, and lets you reply, answer permission
prompts, stop, start and organise sessions without switching windows.

It runs on your computer only (`127.0.0.1`), has no accounts, no cloud and no telemetry, and is a single Python
file for the server plus a single HTML file for the page. No dependencies, no build step.

## Agents

| Agent | How it's driven | What you get |
| --- | --- | --- |
| **Claude Code** | its stream-json mode, plus it watches Claude Code's own session files | everything below, including sessions you started in a terminal or editor |
| **Any agent that speaks the [Agent Client Protocol](https://agentclientprotocol.com)** (ACP) | the board starts it and talks ACP over stdin/stdout | everything for sessions the board starts |

ACP is the open standard editors use to talk to coding agents; a growing list of agents support it directly or through
a small adapter. You add one with a single command (see [docs/ADAPTERS.md](docs/ADAPTERS.md)).

| Feature | Claude Code | ACP agents |
| --- | :-: | :-: |
| Graph, live status, activity stream, sub-sessions | ✓ | ✓ |
| Reply, stop, rename, attachments (images, text; PDFs where the agent takes them) | ✓ | ✓ |
| Message a session while it works: read at its next step (Claude Code) or after the turn (ACP); ⌘/Ctrl+Enter interrupts and answers now | ✓ | ✓ |
| Answer permission prompts on the board | ✓ | ✓ |
| Permission mode menu | ✓ | when the agent offers modes |
| Model menu | ✓ | when the agent offers models (or you list them in config) |
| Effort menu, new sub-agent button, plan-usage meters | ✓ | – (Claude-specific) |
| Watch sessions started elsewhere (terminal, editor) and move them onto the board | ✓ | – (ACP has no way to watch) |
| Resume after a restart | ✓ | if the agent supports `session/resume` or `session/load`; otherwise it starts fresh and says so |
| `/` commands, `@` files | ✓ | `/` commands when the agent reports them; `@` files always |
| Activity statistics, history, trash | ✓ | ✓ (from a transcript the board keeps for them) |

Agents that don't speak ACP can be added as a new backend; see the interface in [docs/ADAPTERS.md](docs/ADAPTERS.md).

## Quick start

Needs Python 3.9 or newer (developed on 3.12) and a browser. macOS and Linux; Windows isn't supported.

```sh
git clone <this repo> session-graph && cd session-graph
bin/session-graph doctor        # what it found on your machine
bin/session-graph open          # starts the helper if needed and opens http://127.0.0.1:8767/
```

Put `bin/` on your PATH if you like (`ln -s "$PWD/bin/session-graph" ~/.local/bin/`).

Add another agent:

```sh
bin/session-graph agents add myagent --label "My Agent" -- myagent --acp
bin/session-graph restart
```

Other commands: `start`, `stop`, `restart`, `status`, `agents`, `where` (prints the data folder).

## Sub-sessions

Every session the board starts knows its own id (`$SESSION_GRAPH_SESSION_ID`) and is told how to split work off:

```sh
session-graph spawn new --name "Build the API" --prompt-file brief.md [--agent claude] [--cwd DIR]
session-graph spawn status
session-graph spawn send <id> "a follow-up"
```

The new session appears under its parent on the board and can use any configured agent. Add
[integrations/agent-instructions.md](integrations/agent-instructions.md) to your agent's instruction file
(`CLAUDE.md`, `AGENTS.md`, …) if you want sessions to split work off on their own.

Claude Code users can also copy [integrations/claude-code/board](integrations/claude-code/board) to
`~/.claude/skills/` for a `/board` command.

## Configuration

Everything the helper writes lives in one data folder (`session-graph where`; default `~/.local/share/session-graph`,
or `$SESSION_GRAPH_HOME`). Nothing is written next to the code. `config.json` is created on first run:

| Key | Default | Meaning |
| --- | --- | --- |
| `user_name` | `""` | how agents refer to you in the context they're given (empty = "the user") |
| `port` | `8767` | the board is at `http://127.0.0.1:<port>/` |
| `folders_root` | `"~"` | sessions can only start in folders inside this one |
| `extra_folders` | `[]` | folders to offer in the new-session menu |
| `default_agent` | `"claude"` | agent new sessions use unless you pick another |
| `agents` | Claude Code | `{id: {type: "claude" \| "acp", label, command, args, env, models, modes, resume_command}}` |
| `usage` | `true` | Claude Code plan-usage meters (no tokens used) |
| `release_check` | `false` | daily "Claude Code update" chip; asks Homebrew's public API |
| `cleanup` | `{"enabled": false, "hour": 4}` | when on, closed sessions' transcripts are deleted from disk at the first check after this hour. **Off by default** |
| `project_groups` | `null` | optional folder of `<name>/project.json` (`{"id","name","path"}`) to group sessions by project |
| `claude_dir` | `null` | Claude Code's config folder if it isn't `$CLAUDE_CONFIG_DIR` or `~/.claude` |

## Safety and privacy

Short version: local only, read-only on your agents' files, nothing leaves your machine except what your agents
themselves send. Details in [docs/PRIVACY.md](docs/PRIVACY.md).

- Listens on `127.0.0.1` only; every request needs the access key from `<data folder>/.token` (mode 600) and a
  matching Host/Origin, so other websites and other users on the machine can't drive it.
- A board session runs with **your** permissions, like any agent session. Permission prompts are answered on the
  board; "Auto" mode is the agent's own auto mode.
- Closed sessions go to Trash and stay on disk unless you turn `cleanup` on.

## Tests

No account or network needed; they use scripted stand-ins for the agents.

```sh
python3 tests/e2e_acp.py       # ACP backend against a mock agent
python3 tests/e2e_claude.py    # Claude Code backend against a mock `claude`
node tests/page_check.mjs http://127.0.0.1:8767/ 'document.title'   # page errors and DOM checks in headless Chrome
python3 tests/scrub_check.py   # looks for personal details before you share a copy
```

## Layout

```
bin/session-graph          command line: start/stop/open/agents/spawn
session_graph/server.py    the helper: scan, claude backend, HTTP + server-sent events
session_graph/acp_backend.py   the ACP backend
session_graph/static/graph.html  the page
docs/  integrations/  tests/
```

Known limits: ACP support was tested against a scripted mock agent that follows the protocol, not against every real
agent (they differ in login, modes, models and how much of the protocol they implement). The page is checked with
headless-browser DOM tests and no console errors, not on every browser. Windows isn't supported.

## Author

Thiwat Chatkham, thiwat.cha.official@gmail.com
