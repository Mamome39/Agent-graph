# Optional: let agents split work off on their own

Paste this into your agent's instruction file (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, …) if you want sessions that run
on the Session Graph board to split clearly separate work into linked sub-sessions without asking. Remove it to keep
that manual. Sessions the board starts are already told the command; this makes it a standing rule.

```markdown
## Splitting work into sub-sessions

When running on the Session Graph board (the environment variable SESSION_GRAPH_SESSION_ID is set) you may split a part
of the work off on your own when it clearly stands alone, for example: plan the whole scheme in one session, then
build each feature in its own. Say so in your reply: what you split off, why, and its name.

- Short, self-contained jobs with one answer (a search, a review): use your own sub-agent feature, if you have one.
- Longer work the user may want to watch or talk to: `session-graph spawn new --name "<short name>" --prompt-file <brief.md>`.
  Check on them with `session-graph spawn status`; follow up with `session-graph spawn send <id> "<message>"`.
- Hand off through a written plan file in the project, with a short brief per sub-session pointing to its part.
  Sub-sessions end each reply with a "Status:" paragraph; read that instead of their transcripts.
- Don't split work that needs your live context, and don't close sessions yourself.
```
