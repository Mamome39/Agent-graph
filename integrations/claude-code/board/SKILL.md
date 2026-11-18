---
name: board
description: Open the Session Graph board, the live graph and control of your coding-agent sessions. Use when the user types /board, or asks to open, reopen or get back to the session graph or the board (for example after closing its tab). Starts the board's helper first if it isn't running.
---

1. Run `session-graph open`. (If the command isn't found, run `bin/session-graph open` from the Session Graph folder, or add that folder's `bin/` to PATH.)
2. Reply in one line: the board is open at the address it printed. If the helper had to start, say that sessions the board ran before are paused and **Resume here** on each one brings it back.

Closing the browser tab never stops a session; only the helper holds them. Never restart or stop the helper from this skill, because that pauses every board session.
