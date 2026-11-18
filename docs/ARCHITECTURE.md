# Architecture

```
 browser page (graph.html) ── HTTP + server-sent events ── helper (server.py) ──┬─ ClaudeOwned ── claude -p (stream-json)
   one file, no build                127.0.0.1 + access key                     │      + watches Claude Code's session files
                                                                                └─ AcpOwned ───── any ACP agent (JSON-RPC over stdio)
                                                                                       + writes its own transcript
```

- **One event model.** Everything the page shows comes from transcript lines (`user` / `assistant` blocks). Claude Code
  writes them itself; the ACP backend writes the same shape. The helper tails the files, keeps a bounded in-memory
  event list and pushes a snapshot + new events to the page on every change.
- **State.** `state.json` holds tracked sessions and everything the board itself decides (names, closed marks, parents,
  defaults). Transcripts are the source of truth for content.
- **Sessions the board owns** (started or resumed from the board) get replies, permission answers and settings through
  the backend. Others are watched (Claude Code only).
- **Capabilities.** Each backend instance exposes `caps`; the page hides menus and buttons an agent can't use.
- **Security.** Loopback only, access key in a mode-600 file injected into the page at serve time, Host and Origin
  checks, folders confined to `folders_root`, request size limits.
