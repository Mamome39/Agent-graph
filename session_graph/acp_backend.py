"""Backend for any agent that speaks the Agent Client Protocol (ACP): JSON-RPC 2.0, one message per line,
over the agent's stdin and stdout. https://agentclientprotocol.com

The board starts the agent as a child process, owns its input, and does what an editor would do:

    initialize -> session/new (or session/resume, session/load) -> session/prompt ... session/update ...
    session/request_permission (the agent asks, the page answers) · session/cancel (the Stop button)

ACP agents keep no transcript the board can watch, so this backend writes the conversation to a transcript of
its own (<data dir>/transcripts/<id>.jsonl) in the same line format the board already reads for Claude Code.
That is what lets the graph, history, statistics and restarts work the same for every agent.

The server module binds itself in with bind(); this file imports nothing from it.
"""
import base64
import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque

H = None  # the server module, set by bind()


def bind(host):
    global H
    H = host


# What the page may rely on for an ACP agent. Model and mode menus appear only when the agent offers them.
CAPS = {"usage": False, "effort": False, "subagents": False, "external": False, "takeover": False,
        "terminal": False, "title": False}

KIND_TOOL = {"read": "Read", "edit": "Edit", "delete": "Edit", "move": "Edit", "execute": "Bash", "search": "Grep",
             "fetch": "WebFetch", "think": "Think", "switch_mode": "Mode"}

MODE_WORDS = {"ask": ("default", "ask", "normal", "manual", "confirm"),
              "auto": ("auto", "yolo", "bypass", "full", "autonomous"),
              "edits": ("accept", "edit", "acceptedits"),
              "plan": ("plan", "read-only", "readonly")}


def iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text_of(block):
    if isinstance(block, dict):
        if block.get("type") == "text":
            return block.get("text") or ""
        if block.get("type") == "resource_link":
            return block.get("name") or block.get("uri") or ""
        if block.get("type") == "resource":
            return (block.get("resource") or {}).get("text") or ""
    return ""


def tool_input(u):
    raw = u.get("rawInput")
    if isinstance(raw, dict):
        inp = dict(raw)
    else:
        inp = {}
    if u.get("title") and "description" not in inp:
        inp["description"] = u["title"]
    for loc in u.get("locations") or []:
        if isinstance(loc, dict) and loc.get("path") and "file_path" not in inp:
            inp["file_path"] = loc["path"]
    return inp


class AcpOwned:
    """One ACP session whose input the board holds. Same surface as the Claude Code backend's session object."""

    kind = "acp"

    def __init__(self, sid, cwd, cfg, agent_id, resume=False, agent_sid=None, mode="ask", context=None, name=None,
                 model=None, effort=None, env=None):
        self.sid, self.cwd, self.cfg, self.agent_id = sid, cwd, cfg, agent_id
        self.ended = False
        self.pending = {}
        self.err = deque(maxlen=20)
        self.commands, self.models, self.suggestion = [], [], ""
        self.modes, self.cur_mode = [], None
        self.agent_sid = agent_sid
        self.want = {"mode": mode, "model": model}
        self.context = context or ""
        self.context_sent = False
        self.resume = resume
        self.caps = dict(CAPS, modes=True, models=bool(cfg.get("models")))
        self.agent_caps = {}
        self.queue = deque()
        self.turn = False
        self.cur_model = None
        self._turn_lock = threading.Lock()
        self.loading = False
        self.ready = threading.Event()
        self.waiters = {}
        self.next_id = 1
        self.wlock = threading.Lock()
        self.buf = {"text": "", "think": "", "t0": 0.0}
        self.calls = {}
        self.tpath = H.own_transcript(sid)
        self.tlock = threading.Lock()
        cmd = [cfg["command_path"]] + [str(a) for a in cfg.get("args") or []]
        child_env = dict(os.environ)
        child_env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
        child_env.update(env or {})
        self.p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True, env=child_env)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        threading.Thread(target=self._handshake, daemon=True).start()

    # ------------------------------------------------------------ wire

    @property
    def status(self):
        """Derived, so it can't disagree with what's actually happening: starting and running a turn both read as working."""
        if self.ended:
            return "ended"
        if self.pending:
            return "waiting"
        if self.turn or self.queue or not self.ready.is_set():
            return "working"
        return "idle"

    def alive(self):
        return self.p.poll() is None

    def _write(self, obj):
        with self.wlock:
            self.p.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
            self.p.stdin.flush()

    def _request(self, method, params, cb=None):
        with self.wlock:
            rid = self.next_id
            self.next_id += 1
        ev, box = threading.Event(), {}
        self.waiters[rid] = (ev, box, cb)
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid, ev, box

    def call(self, method, params, timeout=60):
        _, ev, box = self._request(method, params)
        if not ev.wait(timeout):
            raise TimeoutError("%s timed out" % method)
        if "error" in box:
            e = box["error"] or {}
            raise RuntimeError(str(e.get("message") or e)[:300])
        return box.get("result") or {}

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _read(self):
        for line in self.p.stdout:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            try:
                if "method" in o:
                    self._incoming(o)
                elif "id" in o:
                    w = self.waiters.pop(o["id"], None)
                    if w:
                        if "error" in o:
                            w[1]["error"] = o["error"]
                        else:
                            w[1]["result"] = o.get("result")
                        w[0].set()
                        if w[2]:
                            w[2](o)
            except Exception as e:  # one bad message must not end the session
                self.err.append("bad message: %r" % (e,))
        with H.LOCK:
            self.ended = True
            self.pending.clear()
        for w in list(self.waiters.values()):
            w[1].setdefault("error", {"message": "the agent exited"})
            w[0].set()
        self._flush()
        H.changed()

    def _read_err(self):
        for line in self.p.stderr:
            line = line.strip()
            if line:
                self.err.append(line[:300])

    # ------------------------------------------------------------ handshake

    def _handshake(self):
        try:
            r = self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                         "clientInfo": {"name": "session-graph", "title": "Session Graph", "version": H.VERSION_STR}}, 60)
            self.agent_caps = r.get("agentCapabilities") or {}
            sc = self.agent_caps.get("sessionCapabilities") or {}
            params = {"cwd": self.cwd, "mcpServers": []}
            res = None
            if self.resume and self.agent_sid and sc.get("resume") is not None:
                res = self.call("session/resume", dict(params, sessionId=self.agent_sid), 60)
            elif self.resume and self.agent_sid and self.agent_caps.get("loadSession"):
                self.loading = True
                try:
                    res = self.call("session/load", dict(params, sessionId=self.agent_sid), 120)
                finally:
                    self.loading = False
            if res is not None:
                self.context_sent = True  # the agent already has this session's context
            else:
                if self.resume:
                    self.err.append("This agent can't reopen old sessions, so it started a new one. The history shown is the board's copy.")
                res = self.call("session/new", params, 60)
                self.agent_sid = res.get("sessionId")
                if not self.agent_sid:
                    raise RuntimeError("the agent didn't return a session id")
                self._meta()
            self._take_state(res)
            self._apply_wanted()  # before the first prompt can start, so a "plan" session never runs in the default mode
            self.ready.set()
            H.changed()
            self._pump()
        except Exception as e:
            msg = str(e)
            if "auth" in msg.lower():
                msg += " Sign in to the agent once in a terminal, then resume."
            self.err.append("Couldn't start: " + msg[:240])
            self.ended = True
            self._kill()
            self.ready.set()
            H.changed()

    def _take_state(self, res):
        modes = (res or {}).get("modes") or {}
        if isinstance(modes, dict):
            self.modes = [m for m in modes.get("availableModes") or [] if isinstance(m, dict) and m.get("id")]
            self.cur_mode = modes.get("currentModeId")
        models = (res or {}).get("models") or {}
        if isinstance(models, dict):
            self.models = [{"value": m.get("modelId") or m.get("id"), "label": m.get("name") or m.get("modelId") or m.get("id"),
                            "description": (m.get("description") or "")[:160]}
                           for m in models.get("availableModels") or [] if isinstance(m, dict) and (m.get("modelId") or m.get("id"))]
            self.cur_model = models.get("currentModelId")
        self.caps["modes"] = bool(self.modes) or bool(self.cfg.get("modes"))
        self.caps["models"] = bool(self.models) or bool(self.cfg.get("models"))

    def _apply_wanted(self):
        for what, fn in (("mode", self.set_mode), ("model", self.set_model)):
            v = self.want.get(what)
            if v and not (what == "mode" and v == "ask" and not self.cur_mode):
                try:
                    fn(v)
                except Exception as e:
                    self.err.append("Couldn't set %s: %s" % (what, str(e)[:160]))

    def _meta(self):
        self._emit({"type": "sg-meta", "agent": self.agent_id, "acpSessionId": self.agent_sid, "cwd": self.cwd,
                    "timestamp": iso()}, force=True)
        H.remember_agent_sid(self.sid, self.agent_sid)

    # ------------------------------------------------------------ transcript

    def _emit(self, obj, force=False):
        if self.loading and not force:
            return
        obj.setdefault("cwd", self.cwd)
        obj.setdefault("timestamp", iso())
        with self.tlock:
            os.makedirs(os.path.dirname(self.tpath), exist_ok=True)
            with open(self.tpath, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _assistant(self, block, **extra):
        o = {"type": "assistant", "message": {"role": "assistant", "content": [block]}}
        o.update(extra)
        self._emit(o)

    def _flush(self):
        b = self.buf
        if b["think"]:
            ms = int((time.time() - b["t0"]) * 1000) if b["t0"] else 0
            self._assistant({"type": "thinking", "thinking": ""}, thinkingDurationMs=ms)
            b["think"] = ""
        if b["text"].strip():
            self._assistant({"type": "text", "text": b["text"].strip()})
        b["text"] = ""
        b["t0"] = 0.0

    # ------------------------------------------------------------ what the agent sends

    def _incoming(self, o):
        m, p = o["method"], o.get("params") or {}
        if m == "session/update":
            self._update(p.get("update") or {})
        elif m == "session/request_permission":
            self._permission(o["id"], p)
        elif "id" in o:  # fs/*, terminal/*: not offered, so a polite refusal
            self._write({"jsonrpc": "2.0", "id": o["id"], "error": {"code": -32601, "message": "Not supported by Session Graph."}})

    def _update(self, u):
        kind = u.get("sessionUpdate")
        if kind == "available_commands_update":
            self.commands = [{"name": c.get("name"), "description": (c.get("description") or "")[:220],
                              "hint": ((c.get("input") or {}).get("hint") or "") if isinstance(c.get("input"), dict) else ""}
                             for c in u.get("availableCommands") or [] if isinstance(c, dict) and c.get("name")]
            H.changed()
            return
        if kind == "current_mode_update":
            self.cur_mode = u.get("currentModeId")
            return
        if self.loading:
            return  # session/load replays the whole history; the board already has its own copy
        if kind == "agent_message_chunk":
            self.buf["text"] += text_of(u.get("content"))
            if len(self.buf["text"]) > 4000:
                self._flush()
        elif kind == "agent_thought_chunk":
            if not self.buf["think"]:
                self.buf["t0"] = time.time()
            self.buf["think"] += text_of(u.get("content")) or " "
        elif kind == "tool_call":
            self._flush()
            tid = u.get("toolCallId") or uuid.uuid4().hex[:8]
            name = u.get("name") or KIND_TOOL.get(u.get("kind") or "", "") or (u.get("title") or "Tool")[:30]
            self.calls[tid] = {"title": u.get("title") or name, "name": name}
            self._assistant({"type": "tool_use", "id": tid, "name": name, "input": tool_input(u)})
        elif kind == "tool_call_update":
            c = self.calls.get(u.get("toolCallId"))
            if c is not None and u.get("title"):
                c["title"] = u["title"]
        elif kind == "session_info_update":
            if u.get("title"):
                self._emit({"type": "ai-title", "aiTitle": str(u["title"])[:120]})
        H.changed()

    def _permission(self, rid, p):
        tc = p.get("toolCall") or {}
        known = self.calls.get(tc.get("toolCallId")) or {}
        title = tc.get("title") or known.get("title") or tc.get("kind") or "Tool"
        raw = tc.get("rawInput")
        detail = ""
        if isinstance(raw, dict):
            detail = raw.get("command") or raw.get("file_path") or raw.get("path") or raw.get("url") or ""
            if not detail:
                try:
                    detail = json.dumps(raw, ensure_ascii=False)[:600]
                except Exception:
                    detail = ""
        if not detail:
            detail = ", ".join(l.get("path") for l in tc.get("locations") or [] if isinstance(l, dict) and l.get("path"))
        key = str(rid)
        with H.LOCK:
            self.pending[key] = {"id": key, "tool": str(title)[:80], "input": {}, "detail": str(detail)[:600], "description": "",
                                 "_rpc": rid, "_options": [o for o in p.get("options") or [] if isinstance(o, dict)]}
        H.changed()

    # ------------------------------------------------------------ what the board does

    def send(self, content, now=False):
        """A message sent mid-turn waits its turn (ACP agents take one prompt at a time); now=True stops the turn first."""
        if not self.alive():
            raise H.Busy("The agent isn't running.")
        self.suggestion = ""
        if now and (self.turn or self.pending):
            self.interrupt()
        shown = self._shown(content)
        self.queue.append((shown, self._blocks(content)))
        H.changed()
        if self.ready.is_set():
            self._pump()

    def _shown(self, content):
        if isinstance(content, str):
            return content
        return [b for b in content if isinstance(b, dict)]

    def _blocks(self, content):
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        pc = self.agent_caps.get("promptCapabilities") or {}
        out = []
        for b in content:
            t = b.get("type") if isinstance(b, dict) else None
            if t == "text":
                out.append({"type": "text", "text": b.get("text") or ""})
            elif t == "image" and pc.get("image"):
                src = b.get("source") or {}
                out.append({"type": "image", "data": src.get("data") or "", "mimeType": src.get("media_type") or "image/png"})
            elif t == "image":
                out.append({"type": "text", "text": "[An image was attached, but this agent doesn't accept images.]"})
            elif t == "document":
                out.append({"type": "text", "text": "[A document was attached, but it can't be sent to this agent: %s]" % (b.get("title") or "document")})
        return out or [{"type": "text", "text": ""}]

    def _pump(self):
        """Start the next queued prompt if no turn is running. ACP agents take one prompt at a time."""
        with self._turn_lock:
            if self.turn or not self.queue or not self.ready.is_set() or not self.alive():
                return
            self.turn = True  # first, so status never reads idle between taking the prompt and starting it
            shown, blocks = self.queue.popleft()
        self._emit({"type": "user", "message": {"role": "user", "content": shown if isinstance(shown, str) else self._plain(shown)}})
        if not self.context_sent and self.context:
            blocks = [{"type": "text", "text": self.context + "\n\n---\n"}] + blocks
        self.context_sent = True
        H.changed()
        try:
            self._request("session/prompt", {"sessionId": self.agent_sid, "prompt": blocks}, self._turn_done)
        except Exception as e:
            self.turn = False
            self.err.append("send failed: %s" % str(e)[:200])

    def _plain(self, blocks):
        """What the board shows for a prompt that had attachments (the same shape Claude Code's transcripts use)."""
        return [b for b in blocks if b.get("type") in ("text", "image", "document")]

    def _turn_done(self, o):
        self._flush()
        if "error" in o:
            self.err.append(str((o["error"] or {}).get("message") or "the turn failed")[:300])
        else:
            sr = (o.get("result") or {}).get("stopReason")
            if sr in ("refusal", "max_tokens", "max_turn_requests"):
                self.err.append("Stopped: " + sr.replace("_", " "))
        with self._turn_lock:
            self.turn = False
        H.changed()
        if self.queue:  # off the reader thread: writing a big prompt there could stall against the agent's own output
            threading.Thread(target=self._pump, daemon=True).start()

    def answer(self, rid, allow):
        with H.LOCK:
            req = self.pending.pop(rid, None)
        if req is None:
            raise H.Busy("That request was already answered.")
        want = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
        opt = next((o for k in want for o in req["_options"] if o.get("kind") == k), None)
        outcome = {"outcome": "selected", "optionId": opt.get("optionId")} if opt else {"outcome": "cancelled"}
        self._write({"jsonrpc": "2.0", "id": req["_rpc"], "result": {"outcome": outcome}})
        H.changed()

    def interrupt(self):
        with H.LOCK:
            pend, self.pending = list(self.pending.values()), {}
        for req in pend:  # the protocol requires pending requests to be answered "cancelled"
            self._write({"jsonrpc": "2.0", "id": req["_rpc"], "result": {"outcome": {"outcome": "cancelled"}}})
        self.queue.clear()
        if self.agent_sid:
            self.notify("session/cancel", {"sessionId": self.agent_sid})

    def set_mode(self, board_mode):
        if not self.modes:
            raise H.Busy("This agent doesn't offer permission modes.")
        ids = {m["id"]: m for m in self.modes}
        mapped = (self.cfg.get("modes") or {}).get(board_mode)
        if mapped not in ids:
            mapped = None
            words = MODE_WORDS.get(board_mode) or (board_mode,)
            for exact in (True, False):  # an id that is the word wins over one that merely contains it
                for w in words:
                    for m in self.modes:
                        hay = ("%s %s" % (m["id"], m.get("name") or "")).lower()
                        if board_mode == "auto" and "edit" in hay:
                            continue  # "autoEdit" is accept-edits, not auto
                        if (m["id"].lower() == w) if exact else (w in hay):
                            mapped = m["id"]
                            break
                    if mapped:
                        break
                if mapped:
                    break
        if not mapped:
            raise H.Busy("This agent has no mode that matches \u201c%s\u201d. Map it in config.json (\"modes\")." % board_mode)
        if self.agent_sid:
            self.call("session/set_mode", {"sessionId": self.agent_sid, "modeId": mapped}, 20)
        self.cur_mode = mapped

    def set_model(self, model):
        if not model:
            return
        if not (self.models or self.cfg.get("models")):
            raise H.Busy("This agent doesn't offer a choice of models.")
        self.call("session/set_model", {"sessionId": self.agent_sid, "modelId": model}, 20)

    def set_effort(self, effort):
        if effort:
            raise H.Busy("This agent has no effort setting.")

    def make_title(self, prompt):
        return None

    def rename(self, title):
        pass

    def control(self, request, timeout=30):
        raise H.Busy("This agent can't do that.")

    def _kill(self, sig=None):
        """Signal the agent's whole process group (it was started in its own), so wrappers like npx don't leave children."""
        import signal
        try:
            os.killpg(self.p.pid, sig or signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.p.terminate()
            except Exception:
                pass
        threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self):
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            import signal
            self._kill(signal.SIGKILL)

    def close(self):
        try:
            if self.agent_sid and self.alive() and (self.agent_caps.get("sessionCapabilities") or {}).get("close") is not None:
                self.call("session/close", {"sessionId": self.agent_sid}, 5)
        except Exception:
            pass
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill()
            try:
                self.p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                pass
