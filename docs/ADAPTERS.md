# Adding agents

The board talks to agents through **backends**. Two ship with it:

- `claude`: Claude Code (stream-json mode, plus watching its session files).
- `acp`: any agent that speaks the [Agent Client Protocol](https://agentclientprotocol.com) over stdio.

## An ACP agent

```sh
session-graph agents add <id> --label "<name>" [--model value=Label ...] -- <command> [args...]
session-graph restart
```

or edit `config.json` in the data folder:

```json
"agents": {
  "myagent": {
    "type": "acp",
    "label": "My Agent",
    "command": "myagent",
    "args": ["--acp"],
    "env": {"MYAGENT_NO_TELEMETRY": "1"},
    "models": [{"value": "fast", "label": "Fast"}],
    "modes": {"ask": "default", "plan": "plan", "edits": "acceptEdits", "auto": "yolo"},
    "resume_command": "myagent --resume {session}"
  }
}
```

| Field | Meaning |
| --- | --- |
| `command`, `args`, `env` | how to start the agent in ACP mode. It must be on `PATH` or an absolute path, else the agent isn't offered |
| `models` | optional. Models for the new-session menu. Agents that report their own models in `session/new` don't need it |
| `modes` | optional. Maps the board's four modes (`ask`, `auto`, `edits`, `plan`) to the agent's own mode ids. Without it the board matches by name (`default`/`ask`, `auto`/`yolo`, `accept`/`edit`, `plan`) |
| `resume_command` | optional. Enables the "Navigate to Terminal" button; `{session}` is the agent's own session id |

**Before first use:** the agent must be signed in (ACP agents can't show a login screen on the board). Run it once in a
terminal. If it needs login, the board says so on the session.

### Launch commands people use

These are the commonly documented ways to start well-known agents in ACP mode. They were **not** tested here (only the
scripted mock agent was), and flags change between releases, so check each agent's own documentation:

| Agent | Usually started as |
| --- | --- |
| Gemini CLI | `gemini --experimental-acp` (newer releases: `gemini --acp`) |
| Codex | through a small ACP adapter (for example `npx @zed-industries/codex-acp`) |
| OpenCode | `opencode acp` |
| Goose | `goose acp` |

### What the board does with ACP

`initialize` → `session/new` (or `session/resume` / `session/load` when resuming) → `session/prompt`, reading
`session/update` notifications (message and thought chunks, tool calls, available commands, mode and session-info
updates) and answering `session/request_permission` from the page's Allow / Deny buttons. **Stop** sends `session/cancel`
and answers pending permission requests "cancelled", as the protocol requires. The board advertises no `fs` or
`terminal` client capabilities, so agents use their own file and shell tools.

ACP agents keep no transcript the board can watch, so the board writes one for them
(`<data folder>/transcripts/<id>.jsonl`, same line format Claude Code uses). That's what feeds the graph, history and
statistics, and why history survives restarts even for agents that can't reload sessions.

Limits worth knowing: the board shows a reply when a message segment ends (before a tool call, or when the turn ends),
not word by word; images are sent only if the agent says it accepts them; there's no effort menu or sub-agent button
(they're Claude Code features); sessions started outside the board can't be watched.

## Writing a new backend (not ACP, not Claude Code)

A backend is a class whose instances are one running session. Look at `ClaudeOwned` in `session_graph/server.py` and
`AcpOwned` in `session_graph/acp_backend.py`. The surface the server uses:

```
kind                       "claude" | "acp" | your own
caps                       dict: usage, effort, subagents, external, takeover, terminal, title, modes, models
sid, cwd, p                board session id, folder, the child process (used to wait for shutdown)
status                     "idle" | "working" | "waiting" | "ended"
pending                    {request_id: {id, tool, detail, description, input}}   (keys starting "_" stay private)
err                        deque of short error strings shown on the session
commands, models           lists for the / and model menus
suggestion                 text for Tab-to-accept (or "")
alive()  send(content)  answer(request_id, allow)  interrupt()  close()
set_mode(mode) set_model(m) set_effort(e) make_title(text) rename(title)     raise Busy("…") if unsupported
```

Produce events by appending lines to the session's transcript (`own_transcript(sid)`); the server tails it.
The line format is the one in `acp_backend.py` (`user` / `assistant` with `text`, `thinking`, `tool_use` blocks, and
`ai-title`). Then register the type in `agent_table()` and `make_owned()`.
