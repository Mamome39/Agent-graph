#!/usr/bin/env python3
"""Session Graph: a live control board for the coding-agent sessions on this computer.

    python3 -I server.py serve      run the helper at http://127.0.0.1:<port>/   (default port 8767)

Agents are backends: "claude" (Claude Code, watched and driven through its stream-json mode) and "acp" (any agent
that speaks the Agent Client Protocol, started from the agents list in config.json). See docs/ADAPTERS.md.

What it reads (read-only), for Claude Code only:
  <claude dir>/sessions/*.json         open sessions: pid, name, folder, status (never the .key files)
  <claude dir>/projects/*/<id>.jsonl   each session's transcript, read from where it last stopped
  .../<id>/subagents/agent-*           agents a session started, with their .meta.json

What it runs:
  <agent command>                      sessions started (or resumed) from the board. The board owns their input,
                                       so replies and permission answers go straight in.
  claude -p ... get_usage              Claude Code plan usage every 5 minutes while the page is open (no tokens)
  open / osascript / xdg-open          only for the buttons that open a folder, a terminal or the folder picker

Everything the helper writes lives in one data folder (config.json, state.json, stats.json, transcripts of
non-Claude sessions, attachments). Nothing is written next to this file. See docs/PRIVACY.md.
"""
import base64
import datetime as dt
import glob
import json
import os
import re
import resource
import secrets
import select
import shlex
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import acp_backend  # noqa: E402

VERSION_STR = "1.0.0"

# Sessions started from here must not inherit the environment of whatever agent session (or editor window)
# launched the helper, or they would register as part of it.
for _k in list(os.environ):
    if _k.startswith(("CLAUDE_CODE_", "VSCODE_", "SESSION_GRAPH_SESSION")) or _k in (
            "CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_EFFORT", "CLAUDE_PID",
            "CLAUDE_PROJECT_DIR", "MCP_CONNECTION_NONBLOCKING"):
        del os.environ[_k]

HOME = os.path.expanduser("~")
DATA = os.path.abspath(os.path.expanduser(os.environ.get("SESSION_GRAPH_HOME") or
                                          os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"),
                                                       "session-graph")))
os.makedirs(DATA, mode=0o700, exist_ok=True)
CONFIG_FILE = os.path.join(DATA, "config.json")

DEFAULT_CONFIG = {
    "user_name": "",                 # how agents refer to you in the context they're given; empty = "the user"
    "port": 8767,
    "folders_root": "~",             # sessions may start in any folder inside this one
    "extra_folders": [],             # folders to offer in the new-session menu even before they have a session
    "default_agent": "claude",
    "agents": {
        "claude": {"type": "claude", "label": "Claude Code", "command": "claude"},
    },
    "usage": True,                   # Claude Code plan usage meters (uses no tokens)
    "release_check": False,          # daily "Claude Code update available" chip (asks Homebrew's public API)
    "cleanup": {"enabled": False, "hour": 4},  # delete closed sessions' transcripts at this hour. Off unless you turn it on.
    "project_groups": None,            # optional folder of <name>/project.json files ({"id","name","path"}) that group sessions
    "claude_dir": None,              # default: $CLAUDE_CONFIG_DIR or ~/.claude
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
    except FileNotFoundError:
        user = None
    except Exception as e:
        print("config.json unreadable, using defaults:", e, file=sys.stderr)
        user = {}
    if user is None:  # first run: write the defaults so there is something to edit
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        except OSError:
            pass
        user = {}
    for k, v in user.items():
        if k == "agents" and isinstance(v, dict):
            cfg["agents"].update(v)
        elif k == "cleanup" and isinstance(v, dict):
            cfg["cleanup"].update(v)
        else:
            cfg[k] = v
    return cfg


CFG = load_config()
PORT = int(os.environ.get("SESSION_GRAPH_PORT") or CFG.get("port") or 8767)
HOST = "127.0.0.1"
USER_NAME = (CFG.get("user_name") or "").strip()
THE_USER = USER_NAME or "the user"
ROOT = os.path.realpath(os.path.expanduser(CFG.get("folders_root") or "~"))
CLAUDE_DIR = os.path.realpath(os.path.expanduser(CFG.get("claude_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
SESS_DIR = os.path.join(CLAUDE_DIR, "sessions")
PROJ_DIR = os.path.join(CLAUDE_DIR, "projects")
HUB_DIR = os.path.expanduser(CFG["project_groups"]) if CFG.get("project_groups") else None
STATE_FILE = os.path.join(DATA, "state.json")
PAGE_FILE = os.path.join(HERE, "static", "graph.html")
PROBE_DIR = os.path.join(DATA, "probe")
OWN_TR_DIR = os.path.join(DATA, "transcripts")   # transcripts the board writes for agents that keep none it can watch
CLI = os.path.join(os.path.dirname(HERE), "bin", "session-graph")


def in_root(path):
    return path == ROOT or path.startswith(ROOT.rstrip("/") + "/")


def agent_table():
    """The configured agents that can actually be started here, in config order, with what each can do."""
    out = {}
    for aid, a in (CFG.get("agents") or {}).items():
        if not isinstance(a, dict) or not re.match(r"^[A-Za-z0-9_-]{1,32}$", str(aid)):
            continue
        typ = a.get("type") or "acp"
        path = shutil.which(os.path.expanduser(str(a.get("command") or ""))) if a.get("command") else None
        if typ not in ("claude", "acp") or not path:
            continue
        caps = dict(CLAUDE_CAPS if typ == "claude" else acp_backend.CAPS)
        if typ == "acp":
            caps.update(modes=True, models=bool(a.get("models")), terminal=bool(a.get("resume_command")))
        out[aid] = dict(a, id=aid, type=typ, label=a.get("label") or aid, command_path=path, caps=caps)
    return out


CLAUDE_CAPS = {"usage": True, "effort": True, "subagents": True, "external": True, "takeover": True,
               "terminal": True, "title": True, "modes": True, "models": True}
AGENTS_CFG = agent_table()
CLAUDE = next((a["command_path"] for a in AGENTS_CFG.values() if a["type"] == "claude"), None)
DEFAULT_AGENT = CFG.get("default_agent") if CFG.get("default_agent") in AGENTS_CFG else next(iter(AGENTS_CFG), None)


def load_token():
    """One key per install, kept across restarts, so an open page keeps working after the helper restarts."""
    path = os.path.join(DATA, ".token")
    try:
        with open(path) as f:
            t = f.read().strip()
        if len(t) >= 24:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


TOKEN = load_token()
BOOT = uuid.uuid4().hex[:12]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NAME_RE = re.compile(r"^[\w .\-]{1,60}$")

TAIL_BYTES = 1024 * 1024      # first read of a transcript starts this far from its end
NODE_EVENTS = 150            # events kept per node
FAST, SLOW = 1.0, 10.0       # scan interval with / without the page open
USAGE_EVERY = 300            # seconds between plan usage checks while the page is open
RELEASE_EVERY = 24 * 3600    # daily Claude Code release check while the page is open (when turned on)
CLEANUP_ON = bool((CFG.get("cleanup") or {}).get("enabled"))
CLEANUP_HOUR = int((CFG.get("cleanup") or {}).get("hour", 4)) % 24
AGENT_SHOW_DONE = 3600       # finished agents stay visible this long after their last write
MODES = {"ask": "default", "auto": "auto", "edits": "acceptEdits", "plan": "plan"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_RE = re.compile(r"^[A-Za-z0-9._\-\[\]/:]{1,96}$")
AGENT_TYPES = ("Explore", "Plan", "general-purpose")
RELEASE_URL = "https://formulae.brew.sh/api/cask/claude-code.json"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ATTACH_DIR = os.path.join(DATA, "attachments")
SETTINGS_FILE = os.path.join(CLAUDE_DIR, "settings.json")

LOCK = threading.RLock()
COND = threading.Condition(LOCK)
WAKE = threading.Event()

STATE = {"sessions": {}, "last_seen": None, "last_cleanup": None, "release": None,
         "defaults": {"mode": "ask", "model": "", "effort": ""},  # what new sessions start with on the board
         "folders": {}}  # closed folders: path -> {"closed_at": ts}; their sessions are paused, not closed
STOPPING = {}  # sid -> session object that a folder close is still shutting down
VERSION = 0
CLIENTS = 0
SEQ = 0
EVENTS = deque(maxlen=8000)  # every retained event, in seq order
NODE_EV = {}                 # node id -> deque of its events
TAILS = {}                   # file path -> read offset
TRANSCRIPT = {}              # session id -> transcript path
MISS = {}                    # session id -> last failed transcript lookup
AGENTS = {}                  # session id -> {agent id: info}
OWNER = {}                   # tool_use id -> node id that made the call
TITLES = {}                  # session id -> latest ai title
CUSTOM = {}                  # session id -> name set in the agent (custom-title)
REG = {}                     # session id -> live registry entry
REG_CACHE = {}               # registry filename -> (mtime, data)
OWNED = {}                   # session id -> the running session object (ClaudeOwned or AcpOwned)
IGNORE_PIDS = set()
STARTING = set()             # session ids being resumed right now, so a double click can't start two processes
USAGE = {"data": None, "checked": 0, "error": None, "busy": False}
HUB = {"projects": [], "read": 0}
STATS = {"cpu": 0.0, "rss_mb": 0.0, "t": time.time(), "ru": 0.0}
LAST_SAVE = [0.0]


class Busy(Exception):
    """A request that can't be done right now; the message is shown to the user."""


def now():
    return time.time()


def changed():
    global VERSION
    with COND:
        VERSION += 1
        COND.notify_all()


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        for k in STATE:
            if k in d:
                STATE[k] = d[k]
    except FileNotFoundError:
        pass
    except Exception as e:
        print("state.json unreadable, starting fresh:", e, file=sys.stderr)


def save_state():
    with LOCK:
        data = json.dumps(STATE, indent=1)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, STATE_FILE)
    LAST_SAVE[0] = now()


# ---------------------------------------------------------------- helpers

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def pid_gone(pid):
    """True once a process has exited (an exited child nobody has collected yet counts as gone)."""
    if not pid_alive(pid):
        return True
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
        return not st or st.startswith("Z")
    except Exception:
        return False


def enc_dir(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def tilde(p):
    p = p or ""
    return "~" + p[len(HOME):] if p == HOME or p.startswith(HOME + "/") else p


def folder_name(path):
    return "home" if path == HOME else (os.path.basename(path.rstrip("/")) or path)


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return now()


def fmt_secs(ms):
    if not ms:
        return ""
    return "<1s" if ms < 1000 else "%ds" % round(ms / 1000)


def short_path(p):
    p = tilde(p or "")
    parts = p.split("/")
    return p if len(parts) <= 3 else "…/" + "/".join(parts[-2:])


def tool_summary(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return inp.get("description") or (inp.get("command") or "")[:160]
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return short_path(inp.get("file_path") or inp.get("notebook_path"))
    if name == "Grep":
        return '"%s"' % (inp.get("pattern") or "")[:80]
    if name == "Glob":
        return inp.get("pattern") or ""
    if name in ("Agent", "Task"):
        return "%s: %s" % (inp.get("subagent_type") or "agent", inp.get("description") or "")
    if name == "WebSearch":
        return (inp.get("query") or "")[:120]
    if name == "WebFetch":
        return (inp.get("url") or "")[:160]
    if name == "Skill":
        return inp.get("skill") or ""
    for k in ("description", "query", "prompt", "url", "path"):
        if isinstance(inp.get(k), str) and inp[k]:
            return inp[k][:120]
    return ""


def perm_detail(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return inp.get("command") or ""
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return tilde(inp.get("file_path") or inp.get("notebook_path") or "")
    if name == "WebFetch":
        return inp.get("url") or ""
    try:
        return json.dumps(inp, ensure_ascii=False)[:600]
    except Exception:
        return ""


TAG_SKIP = ("<system-reminder>", "<local-command", "Caveat:", "<task-notification>", "<command-message>",
            "<user-memory", "[Request interrupted")


def prompt_text(content):
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return None
    extras = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image":
                extras.append("image")
            elif isinstance(b, dict) and b.get("type") == "document":
                extras.append(b.get("title") or "document")
    out = []
    for t in parts:
        t = (t or "").strip()
        names = re.findall(r'<attachment name="([^"]*)"', t)
        if names:
            extras.extend(names)
            t = re.sub(r"<attachment\b[^>]*>.*?</attachment>", "", t, flags=re.S).strip()
        if not t:
            continue
        m = re.search(r"<command-name>(.*?)</command-name>", t, re.S)
        if m:
            a = re.search(r"<command-args>(.*?)</command-args>", t, re.S)
            out.append((m.group(1).strip() + " " + (a.group(1).strip() if a else "")).strip())
            continue
        if t.startswith(TAG_SKIP):
            continue
        t = re.sub(r"<(ide_selection|ide_opened_file|system-reminder|ide_diagnostics)>.*?</\1>", "", t, flags=re.S).strip()
        if t and not t.startswith("<ide_"):
            out.append(t)
    text = "\n".join(out)
    if extras:
        imgs = extras.count("image")
        bits = (["%d image%s" % (imgs, "" if imgs == 1 else "s")] if imgs else []) + [x for x in extras if x != "image"]
        text = (text + "\n" if text else "") + "Attached: " + ", ".join(bits)
    return text or None


def scope_label(scope):
    if isinstance(scope, str):
        return scope
    if isinstance(scope, dict):
        for k in ("label", "display_label", "display_name", "name", "model", "surface"):
            v = scope.get(k)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                inner = scope_label(v)
                if inner:
                    return inner
    return ""


def version_tuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])
    except Exception:
        return ()


# ---------------------------------------------------------------- projects hub

def hub_projects():
    if now() - HUB["read"] < 60:
        return HUB["projects"]
    out = []
    for p in (glob.glob(os.path.join(HUB_DIR, "*", "project.json")) if HUB_DIR else []):
        if "/_removed/" in p:
            continue
        try:
            with open(p) as f:
                d = json.load(f)
        except Exception:
            continue
        path = os.path.realpath(os.path.expanduser(d.get("path") or ""))
        if d.get("path") and os.path.isdir(path):
            out.append({"id": d.get("id") or os.path.basename(os.path.dirname(p)), "name": d.get("name") or "", "path": path})
    HUB.update(projects=out, read=now())
    return out


def project_for(cwd):
    best = None
    for p in hub_projects():
        if cwd == p["path"] or cwd.startswith(p["path"] + "/"):
            if best is None or len(p["path"]) > len(best["path"]):
                best = p
    return best


# ---------------------------------------------------------------- events and transcripts

def add_event(node, sid, kind, text, ts, tool=None):
    global SEQ
    SEQ += 1
    e = {"seq": SEQ, "node": node, "sid": sid, "kind": kind, "text": text, "ts": ts}
    if tool:
        e["tool"] = tool
    EVENTS.append(e)
    NODE_EV.setdefault(node, deque(maxlen=NODE_EVENTS)).append(e)


def parse_line(node, sid, d):
    t = d.get("type")
    if t == "ai-title":
        if node.startswith("s:") and d.get("aiTitle"):
            TITLES[sid] = d["aiTitle"]
            return True
        return False
    if t == "custom-title":
        if node.startswith("s:") and d.get("customTitle"):
            CUSTOM[sid] = d["customTitle"]
            return True
        return False
    if t not in ("user", "assistant") or d.get("isMeta"):
        return False
    m = d.get("message") or {}
    ts = parse_ts(d.get("timestamp")) if d.get("timestamp") else now()
    if t == "user":
        if node.startswith("a:") and not d.get("parentUuid"):
            return False  # an agent's task prompt; its description is on the node already
        txt = prompt_text(m.get("content"))
        if not txt:
            return False
        add_event(node, sid, "prompt", txt[:3000], ts)
        return True
    hit = False
    for b in m.get("content") or []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and (b.get("text") or "").strip():
            add_event(node, sid, "reply", b["text"].strip()[:6000], ts)
            hit = True
        elif bt == "tool_use":
            if b.get("id"):
                OWNER[b["id"]] = node
            add_event(node, sid, "tool", tool_summary(b.get("name", ""), b.get("input")), ts, tool=b.get("name") or "tool")
            hit = True
        elif bt == "thinking":
            add_event(node, sid, "think", ("thought " + fmt_secs(d.get("thinkingDurationMs"))).strip(), ts)
            hit = True
    return hit


def tail(path, node, sid):
    """Parse the lines appended to path since the last call. Returns True if anything showed up."""
    try:
        size = os.stat(path).st_size
    except OSError:
        return False
    off = TAILS.get(path)
    skip_first = False
    if off is None:
        off = max(0, size - TAIL_BYTES)
        skip_first = off > 0
    elif size < off:
        off = 0
    if size == off:
        TAILS[path] = off
        return False
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read(size - off)
    nl = data.rfind(b"\n")
    if nl < 0:
        TAILS.setdefault(path, off)
        return False
    chunk = data[:nl + 1]
    TAILS[path] = off + len(chunk)
    lines = chunk.split(b"\n")
    if skip_first:
        lines = lines[1:]
    hit = False
    for raw in lines:
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if parse_line(node, sid, d):
            hit = True
    return hit


def find_transcript(sid, cwd):
    p = TRANSCRIPT.get(sid)
    if p and os.path.exists(p):
        return p
    own = own_transcript(sid)
    if os.path.exists(own):
        TRANSCRIPT[sid] = own
        return own
    cand = os.path.join(PROJ_DIR, enc_dir(cwd), sid + ".jsonl")
    if os.path.exists(cand):
        TRANSCRIPT[sid] = cand
        return cand
    if now() - MISS.get(sid, 0) < 15:
        return None
    hits = glob.glob(os.path.join(PROJ_DIR, "*", sid + ".jsonl"))
    if hits:
        TRANSCRIPT[sid] = hits[0]
        return hits[0]
    MISS[sid] = now()
    return None


def scan_agents(sid, tpath):
    d = tpath[:-len(".jsonl")] + "/subagents"
    if not os.path.isdir(d):
        return False
    hit = False
    ags = AGENTS.setdefault(sid, {})
    for fn in os.listdir(d):
        if not (fn.startswith("agent-") and fn.endswith(".meta.json")):
            continue
        aid = fn[len("agent-"):-len(".meta.json")]
        mp = os.path.join(d, fn)
        try:
            mt = os.stat(mp).st_mtime
        except OSError:
            continue
        a = ags.get(aid)
        if a is None or a.get("meta_mtime") != mt:
            try:
                with open(mp) as f:
                    meta = json.load(f)
            except Exception:
                continue
            a = ags.setdefault(aid, {"id": aid})
            a.update(meta_mtime=mt, type=meta.get("agentType") or "agent", desc=meta.get("description") or "",
                     finished=bool(meta.get("finished")), depth=meta.get("spawnDepth") or 1, tool_use=meta.get("toolUseId"))
            hit = True
        jp = os.path.join(d, "agent-%s.jsonl" % aid)
        try:
            a["mtime"] = os.stat(jp).st_mtime
        except OSError:
            a.setdefault("mtime", mt)
            continue
        if tail(jp, "a:" + aid, sid):
            hit = True
    return hit


def drop_session_data(sid):
    for aid in list((AGENTS.pop(sid, None) or {}).keys()):
        NODE_EV.pop("a:" + aid, None)
    NODE_EV.pop("s:" + sid, None)
    p = TRANSCRIPT.pop(sid, None)
    if p:
        TAILS.pop(p, None)
        sub = p[:-len(".jsonl")] + "/subagents/"
        for k in [k for k in TAILS if k.startswith(sub)]:
            TAILS.pop(k, None)
    TITLES.pop(sid, None)
    CUSTOM.pop(sid, None)


# ---------------------------------------------------------------- registry and scan

def scan_registry():
    live = {}
    try:
        names = os.listdir(SESS_DIR)
    except OSError:
        names = []
    seen = set()
    for fn in names:
        if not fn.endswith(".json"):
            continue  # skip the .key files: they are credentials, not status
        seen.add(fn)
        p = os.path.join(SESS_DIR, fn)
        try:
            mt = os.stat(p).st_mtime
        except OSError:
            continue
        c = REG_CACHE.get(fn)
        if not c or c[0] != mt:
            try:
                with open(p) as f:
                    c = (mt, json.load(f))
            except Exception:
                continue
            REG_CACHE[fn] = c
        e = c[1]
        pid, sid = e.get("pid"), e.get("sessionId")
        if not sid or pid in IGNORE_PIDS or (e.get("cwd") or "").startswith(PROBE_DIR):
            continue
        if pid_alive(pid):
            live[sid] = e
    for fn in list(REG_CACHE):
        if fn not in seen:
            del REG_CACHE[fn]
    return live


def new_record(sid, cwd, **kw):
    r = {"sid": sid, "cwd": cwd or HOME, "name": "", "first_seen": now(), "closed_at": None,
         "origin": "", "entrypoint": "", "kind": "", "mode": None}
    r.update(kw)
    return r


def scan():
    global REG
    live = scan_registry()
    hit = dirty = False
    with LOCK:
        for sid, e in live.items():
            s = STATE["sessions"].get(sid)
            if s is None:
                if not find_transcript(sid, e.get("cwd")):
                    continue  # nothing to show until it has a transcript (helper processes never get one)
                STATE["sessions"][sid] = new_record(sid, e.get("cwd"), name=e.get("name") or "",
                                                     entrypoint=e.get("entrypoint") or "", kind=e.get("kind") or "",
                                                     name_source=e.get("nameSource") or "")
                hit = dirty = True
                continue
            if s.get("closed_at") and (e.get("startedAt") or 0) / 1000 > s["closed_at"]:
                s["closed_at"] = None  # you reopened it somewhere after closing it here
                hit = dirty = True
            for k in ("name", "cwd", "entrypoint", "kind"):
                v = e.get(k)
                if v and s.get(k) != v and not (s.get("origin") == "board" and k in ("entrypoint", "kind")):
                    s[k] = v
                    hit = dirty = True
            ns = e.get("nameSource") or ""
            if e.get("name") and s.get("name_source") != ns:
                s["name_source"] = ns
                hit = dirty = True
        if set(live) != set(REG) or any(live[k].get("status") != REG[k].get("status") for k in live if k in REG):
            hit = True
        REG = live
        for sid, s in list(STATE["sessions"].items()):
            tp = find_transcript(sid, s["cwd"])
            if not tp:
                if sid not in live and not owned_alive(sid) and now() - (s.get("first_seen") or 0) > 120:
                    del STATE["sessions"][sid]  # it ended without ever writing a transcript
                    drop_session_data(sid)
                    hit = dirty = True
                continue
            if tail(tp, "s:" + sid, sid):
                hit = True
            if scan_agents(sid, tp):
                hit = True
        STATE["last_seen"] = now()
    if dirty or now() - LAST_SAVE[0] > 60:
        save_state()
    if hit:
        changed()


def catch_up():
    """Track sessions that ran while the helper was off (their transcript changed since it last ran)."""
    since = STATE.get("last_seen")
    if not since:
        return
    for p in glob.glob(os.path.join(PROJ_DIR, "*", "*.jsonl")):
        sid = os.path.basename(p)[:-len(".jsonl")]
        if not UUID_RE.match(sid) or sid in STATE["sessions"]:
            continue
        try:
            mt = os.stat(p).st_mtime
        except OSError:
            continue
        if mt <= since:
            continue
        cwd = None
        try:
            with open(p) as f:
                for _, line in zip(range(40), f):
                    try:
                        cwd = json.loads(line).get("cwd")
                    except Exception:
                        continue
                    if cwd:
                        break
        except OSError:
            continue
        if not cwd or cwd.startswith(PROBE_DIR):
            continue
        STATE["sessions"][sid] = new_record(sid, cwd, first_seen=mt)
        TRANSCRIPT[sid] = p


def delete_session_files(sid):
    """Delete everything an agent keeps for one session. Only exact paths named after this session id."""
    if not UUID_RE.match(sid):
        return []
    base = CLAUDE_DIR
    tp = TRANSCRIPT.get(sid) or next(iter(glob.glob(os.path.join(PROJ_DIR, "*", sid + ".jsonl"))), None)
    targets = []
    if tp:
        targets += [tp, tp[:-len(".jsonl")]]
    targets += [own_transcript(sid)]
    targets += [os.path.join(base, d, sid) for d in ("session-env", "file-history", "tasks")]
    targets += [os.path.join(base, "debug", sid + ".txt")] + glob.glob(os.path.join(base, "todos", sid + "*.json"))
    if tp and os.path.exists(tp):
        stats_keep(tp, sid)
    elif os.path.exists(own_transcript(sid)):
        stats_keep(own_transcript(sid), sid)
    roots = (base + "/", OWN_TR_DIR + "/")
    gone = []
    for t in targets:
        if not os.path.lexists(t) or not t.startswith(roots) or sid not in os.path.basename(t):
            continue
        try:
            if os.path.isdir(t) and not os.path.islink(t):
                shutil.rmtree(t)
            else:
                os.remove(t)
            gone.append(tilde(t))
        except OSError as e:
            print("cleanup could not delete %s: %r" % (t, e), file=sys.stderr)
    return gone


def maybe_cleanup():
    if not CLEANUP_ON:
        return
    t = dt.datetime.now()
    today = t.strftime("%Y-%m-%d")
    if t.hour < CLEANUP_HOUR or STATE.get("last_cleanup") == today:
        return
    with LOCK:
        closed = [(sid, dict(s)) for sid, s in STATE["sessions"].items() if s.get("closed_at")]
        STATE["last_cleanup"] = today
    deleted = 0
    for sid, s in closed:
        if sid in REG or owned_alive(sid):
            continue  # still running somewhere; it stays closed and goes at the next cleanup
        if s.get("kind") == "bg" and CLAUDE:
            subprocess.run([CLAUDE, "rm", sid], capture_output=True, timeout=30)
        paths = delete_session_files(sid)
        if UUID_RE.match(sid) and os.path.isdir(os.path.join(ATTACH_DIR, sid)):
            shutil.rmtree(os.path.join(ATTACH_DIR, sid), ignore_errors=True)
            paths.append(tilde(os.path.join(ATTACH_DIR, sid)))
        with open(os.path.join(DATA, "cleanup.log"), "a") as f:
            f.write("%s  %s  %s\n    %s\n" % (t.strftime("%Y-%m-%d %H:%M"), sid, display_name(sid, s), "\n    ".join(paths) or "(no files)"))
        with LOCK:
            STATE["sessions"].pop(sid, None)
            drop_session_data(sid)
        deleted += 1
    save_state()
    if deleted:
        changed()


# ---------------------------------------------------------------- sessions the board owns

def build_context(cwd, parent=None):
    who = USER_NAME or "The user"
    ctx = ("This session was started from %sSession Graph board, a local page. %s reads your "
           "replies and answers permission prompts there. Ask questions in plain text in your reply."
           % (USER_NAME + "'s " if USER_NAME else "a ", who))
    ctx += ("\n\nIf part of the work clearly stands alone, you may split it off into a linked sub-session on the board with "
            "`%s spawn new --name \"<short name>\" --prompt-file <brief.md>` (check on it with `%s spawn status`, "
            "follow up with `%s spawn send <id> \"<message>\"`). Put the plan in a file and keep the brief short."
            % (CLI, CLI, CLI))
    if parent:
        ps = STATE["sessions"].get(parent)
        ctx += ("\n\nYou are a sub-session that the session \"%s\" (id %s) split off to handle one part of its work. "
                "Stay within the brief in your first message; if it points to a blueprint or plan file, read your part "
                "of it. End each reply with a short paragraph that starts with \"Status:\" (what you did, what is left, "
                "files changed). The parent session reads that with `%s spawn status`."
                % (display_name(parent, ps) if ps else parent[:8], parent, CLI))
    return ctx


GLOBAL_INIT = {"commands": [], "models": []}  # from the usage check, for composers that have no session yet


def init_lists(resp):
    """Commands and models from an initialize response."""
    models = []
    for m in resp.get("models") or []:
        if isinstance(m, dict):
            v = m.get("value") or m.get("id") or m.get("model")
            if v:
                models.append({"value": v, "label": m.get("displayName") or m.get("display_name") or m.get("label") or v,
                               "description": (m.get("description") or "")[:160]})
    commands = [{"name": c.get("name"), "description": (c.get("description") or "")[:220], "hint": c.get("argumentHint") or ""}
                for c in resp.get("commands") or [] if isinstance(c, dict) and c.get("name")]
    return commands, models


class ClaudeOwned:
    """A Claude Code session whose input the board holds (stream-json mode)."""

    kind = "claude"

    def __init__(self, sid, cwd, resume=False, mode="ask", context=None, name=None, model=None, effort=None, env=None):
        self.sid, self.cwd = sid, cwd
        self.caps = dict(CLAUDE_CAPS)
        self.status = "idle"
        self.started = False  # the first init frame is the process starting, later ones are queued turns
        self.pending = {}
        self.err = deque(maxlen=20)
        self.wlock = threading.Lock()
        self.waiters = {}
        cmd = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--permission-prompt-tool", "stdio", "--permission-mode", MODES.get(mode, "default"),
               "--disallowed-tools", "AskUserQuestion"]
        cmd += ["--resume", sid] if resume else ["--session-id", sid]
        if name:
            cmd += ["-n", name]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if context:
            cmd += ["--append-system-prompt", context]
        self.commands = []
        self.models = []
        self.suggestion = ""
        child_env = dict(os.environ)
        child_env.update(env or {})
        self.p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, bufsize=1, start_new_session=True, env=child_env)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        ev, box = threading.Event(), {}
        self.waiters["init"] = (ev, box)
        self._write({"type": "control_request", "request_id": "init",
                     "request": {"subtype": "initialize", "promptSuggestions": True}})
        threading.Thread(target=self._after_init, args=(ev, box), daemon=True).start()

    def _after_init(self, ev, box):
        if not ev.wait(60):
            return
        r = box.get("r") or {}
        if r.get("subtype") == "success":
            resp = r.get("response") or {}
            for m in resp.get("models") or []:
                if not isinstance(m, dict):
                    continue
                v = m.get("value") or m.get("id") or m.get("model")
                if v:
                    self.models.append({"value": v, "label": m.get("displayName") or m.get("display_name") or m.get("label") or v,
                                        "description": (m.get("description") or "")[:160]})
            self.commands = [{"name": c.get("name"), "description": (c.get("description") or "")[:220],
                              "hint": c.get("argumentHint") or ""}
                             for c in (r.get("response") or {}).get("commands") or [] if c.get("name")]

    def alive(self):
        return self.p.poll() is None

    def _write(self, obj):
        with self.wlock:
            self.p.stdin.write(json.dumps(obj) + "\n")
            self.p.stdin.flush()

    def send(self, text, now=False):
        """Send a message. While a turn is running it must not wait for the turn to finish: priority "next"
        slips it in at the next tool boundary (what typing into the terminal does), "now" interrupts the turn."""
        self.suggestion = ""
        msg = {"type": "user", "message": {"role": "user", "content": text},
               "parent_tool_use_id": None, "session_id": self.sid}
        with LOCK:
            busy = self.status in ("working", "waiting")
        if busy:
            msg["priority"] = "now" if now else "next"
        self._write(msg)
        with LOCK:
            if busy and now:
                self.pending.clear()  # the interrupted turn no longer waits on those
            self.status = "working"
        changed()

    def answer(self, rid, allow):
        with LOCK:
            req = self.pending.pop(rid, None)
        if req is None:
            raise Busy("That request was already answered.")
        resp = ({"behavior": "allow", "updatedInput": req["input"]} if allow else
                {"behavior": "deny", "message": "%s denied this from the Session Graph board." % THE_USER.capitalize()})
        self._write({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": resp}})
        with LOCK:
            self.status = "waiting" if self.pending else "working"
        changed()

    def control(self, request, timeout=30):
        """Send a control request and wait for its response."""
        rid = "req-" + uuid.uuid4().hex[:10]
        ev, box = threading.Event(), {}
        self.waiters[rid] = (ev, box)
        self._write({"type": "control_request", "request_id": rid, "request": request})
        if not ev.wait(timeout):
            self.waiters.pop(rid, None)
            raise TimeoutError("%s timed out" % request.get("subtype"))
        r = box.get("r") or {}
        if r.get("subtype") != "success":
            raise RuntimeError(r.get("error") or "%s failed" % request.get("subtype"))
        return r.get("response") or {}

    def interrupt(self):
        self._write({"type": "control_request", "request_id": "int-" + uuid.uuid4().hex[:8],
                     "request": {"subtype": "interrupt"}})

    def set_mode(self, board_mode):
        self.control({"subtype": "set_permission_mode", "mode": MODES[board_mode]})

    def set_model(self, model):
        self.control({"subtype": "set_model", "model": model or None})

    def set_effort(self, effort):
        self.control({"subtype": "apply_flag_settings", "settings": {"effortLevel": effort or None}})

    def make_title(self, prompt):
        r = safe_control(self, {"subtype": "generate_session_title", "description": prompt[:3000], "persist": False}, 60)
        return (r or {}).get("title")

    def rename(self, title):
        safe_control(self, {"subtype": "rename_session", "title": title, "source": "host"})

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.p.terminate()

    def _read(self):
        for line in self.p.stdout:
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t == "control_request":
                req = o.get("request") or {}
                rid = o.get("request_id")
                if req.get("subtype") == "can_use_tool":
                    name = req.get("tool_name") or ""
                    inp = req.get("input") or {}
                    with LOCK:
                        self.pending[rid] = {"id": rid, "tool": req.get("display_name") or name, "input": inp,
                                             "detail": perm_detail(name, inp), "description": req.get("description") or ""}
                        self.status = "waiting"
                    changed()
                else:
                    try:
                        self._write({"type": "control_response", "response": {
                            "subtype": "error", "request_id": rid, "error": "Not supported by Session Graph."}})
                    except Exception:
                        pass
            elif t == "system" and o.get("subtype") == "init":
                # a queued message starts its own turn right after the previous result: show it as working again
                with LOCK:
                    if self.started and self.status == "idle":
                        self.status = "working"
                    self.started = True
                changed()
            elif t == "prompt_suggestion":
                self.suggestion = (o.get("suggestion") or "")[:500]
                changed()
            elif t == "control_response":
                r = o.get("response") or {}
                w = self.waiters.pop(r.get("request_id"), None)
                if w:
                    w[1]["r"] = r
                    w[0].set()
            elif t == "result":
                with LOCK:
                    self.status = "waiting" if self.pending else "idle"
                    if o.get("is_error"):
                        self.err.append(str(o.get("result") or o.get("subtype") or "error")[:300])
                changed()
        with LOCK:
            self.status = "ended"
            self.pending.clear()
        for ev, _ in list(self.waiters.values()):
            ev.set()
        changed()

    def _read_err(self):
        for line in self.p.stderr:
            line = line.strip()
            if line:
                self.err.append(line[:300])


def owned_alive(sid):
    o = OWNED.get(sid)
    return o if (o and o.alive()) else None


def session_env(sid):
    """What every board-started agent gets, so a command it runs (`session-graph spawn`) knows which session it is."""
    return {"SESSION_GRAPH_SESSION_ID": sid, "SESSION_GRAPH_URL": "http://%s:%d" % (HOST, PORT), "SESSION_GRAPH_HOME": DATA}


def agent_of(s):
    a = (s or {}).get("agent") or "claude"
    return a if a in AGENTS_CFG else None


def make_owned(sid, cwd, agent, resume=False, mode="ask", context=None, name=None, model=None, effort=None, agent_sid=None):
    cfg = AGENTS_CFG.get(agent)
    if not cfg:
        raise ValueError("That agent isn't installed or isn't set up in config.json.")
    if cfg["type"] == "claude":
        env = session_env(sid)
        if CFG.get("claude_dir"):
            env["CLAUDE_CONFIG_DIR"] = CLAUDE_DIR
        return ClaudeOwned(sid, cwd, resume=resume, mode=mode, context=context, name=name, model=model, effort=effort, env=env)
    return acp_backend.AcpOwned(sid, cwd, cfg, agent, resume=resume, agent_sid=agent_sid, mode=mode, context=context,
                                name=name, model=model, effort=effort, env=session_env(sid))


def own_transcript(sid):
    return os.path.join(OWN_TR_DIR, sid + ".jsonl")


def remember_agent_sid(sid, agent_sid):
    with LOCK:
        s = STATE["sessions"].get(sid)
        if s is not None:
            s["agent_sid"] = agent_sid
    save_state()


# ---------------------------------------------------------------- usage and releases

def usage_enabled():
    return bool(CLAUDE and CFG.get("usage", True))


def fetch_usage():
    if not usage_enabled():
        return
    with LOCK:
        if USAGE["busy"]:
            return
        USAGE["busy"] = True
    p = None
    try:
        os.makedirs(PROBE_DIR, exist_ok=True)
        env = dict(os.environ, CLAUDE_CONFIG_DIR=CLAUDE_DIR) if CFG.get("claude_dir") else None
        p = subprocess.Popen([CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                              "--verbose", "--no-session-persistence"], cwd=PROBE_DIR, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True, env=env)
        IGNORE_PIDS.add(p.pid)
        p.stdin.write(json.dumps({"type": "control_request", "request_id": "i", "request": {"subtype": "initialize"}}) + "\n")
        p.stdin.write(json.dumps({"type": "control_request", "request_id": "u", "request": {"subtype": "get_usage"}}) + "\n")
        p.stdin.flush()
        got, deadline, buf = None, now() + 30, b""
        fd = p.stdout.fileno()  # read the raw pipe: select() can't see lines already sitting in a text wrapper's buffer
        while now() < deadline and got is None:
            r, _, _ = select.select([fd], [], [], 1)
            if not r:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf and got is None:
                line, buf = buf.split(b"\n", 1)
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                resp = o.get("response") or {}
                if o.get("type") == "control_response" and resp.get("request_id") == "i":
                    cmds, models = init_lists(resp.get("response") or {})
                    if cmds:
                        GLOBAL_INIT.update(commands=cmds, models=models)
                elif o.get("type") == "control_response" and resp.get("request_id") == "u":
                    got = resp
        if not got or got.get("subtype") != "success":
            raise RuntimeError("Claude Code didn't return usage")
        r = got.get("response") or {}
        rl = r.get("rate_limits") or {}
        limits = [{"kind": x.get("kind"), "group": x.get("group"), "percent": x.get("percent"),
                   "severity": x.get("severity"), "resets_at": x.get("resets_at"),
                   "label": scope_label(x.get("scope")), "active": x.get("is_active")}
                  for x in (rl.get("limits") or [])]
        if not limits:
            for k, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
                v = rl.get(k)
                if isinstance(v, dict):
                    limits.append({"kind": kind, "percent": v.get("utilization"), "resets_at": v.get("resets_at"),
                                   "severity": "normal", "label": ""})
        with LOCK:
            USAGE.update(data={"plan": r.get("subscription_type"), "limits": limits,
                               "available": r.get("rate_limits_available")}, checked=now(), error=None)
    except Exception as e:
        with LOCK:
            USAGE.update(error=str(e)[:200], checked=now())
    finally:
        if p:
            try:
                p.stdin.close()
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
        with LOCK:
            USAGE["busy"] = False
        changed()


def check_release():
    if not CLAUDE:
        return
    rel = dict(STATE.get("release") or {})
    rel["checked"] = now()
    with LOCK:
        STATE["release"] = rel
    try:
        req = urllib.request.Request(RELEASE_URL, headers={"User-Agent": "session-graph/" + VERSION_STR})
        with urllib.request.urlopen(req, timeout=15) as r:
            latest = json.load(r).get("version")
        out = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        rel.update(latest=latest, installed=(out.split() or [""])[0], error=None)
    except Exception as e:
        rel["error"] = str(e)[:200]
    with LOCK:
        STATE["release"] = rel
    save_state()
    changed()


def update_stats():
    t = now()
    if t - STATS["t"] < 10:
        return False
    ru = resource.getrusage(resource.RUSAGE_SELF)
    cpu = ru.ru_utime + ru.ru_stime
    STATS["cpu"] = round(100 * (cpu - STATS["ru"]) / (t - STATS["t"]), 2)
    STATS["t"], STATS["ru"] = t, cpu
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, timeout=5).stdout
        STATS["rss_mb"] = round(int(out.strip()) / 1024, 1)
    except Exception:
        pass
    return True


# ---------------------------------------------------------------- snapshot for the page

DEFAULTS = {"t": 0, "v": {}}


def user_defaults():
    if now() - DEFAULTS["t"] > 60:
        try:
            with open(SETTINGS_FILE) as f:
                d = json.load(f)
        except Exception:
            d = {}
        DEFAULTS.update(t=now(), v={"model": d.get("model") or "", "effort": d.get("effortLevel") or ""})
    return DEFAULTS["v"]


def session_caps(o, s):
    if o:
        return o.caps
    a = AGENTS_CFG.get(agent_of(s)) or {}
    return a.get("caps") or {}


def can_pick_folder():
    return sys.platform == "darwin" or bool(shutil.which("zenity"))


def session_status(sid, s):
    if s.get("closed_at"):
        return "closed"
    o = owned_alive(sid)
    if o:
        return "waiting" if o.pending else ("working" if o.status == "working" else "idle")
    e = REG.get(sid)
    if e:
        return {"busy": "working", "waiting": "waiting"}.get(e.get("status"), "idle")
    return "paused"


def snapshot():
    with LOCK:
        t = now()
        folders, sessions, agents = {}, [], []
        hidden, shut = set(), []  # closed folders are untracked: they and their sessions leave the graph
        for fk, v in STATE["folders"].items():
            grp = folder_group(fk)
            if grp:
                hidden.update(grp)
                proj = project_for(fk)
                shut.append({"abs": fk, "path": tilde(fk), "name": (proj["name"] or proj["id"]) if proj else folder_name(fk),
                             "n": len(grp), "closed_at": v.get("closed_at")})
        for sid, s in sorted(STATE["sessions"].items(), key=lambda kv: kv[1].get("first_seen") or 0):
            if sid in hidden:
                continue
            st = session_status(sid, s)
            proj = project_for(s["cwd"])
            fkey = proj["path"] if proj else s["cwd"]
            if fkey not in folders:
                folders[fkey] = {"id": "f:" + fkey, "path": tilde(fkey), "abs": fkey, "project": proj["id"] if proj else None,
                                 "name": (proj["name"] or proj["id"]) if proj else folder_name(fkey)}
            o = owned_alive(sid)
            e = REG.get(sid) or {}
            ags = AGENTS.get(sid) or {}
            visible = {aid for aid, a in ags.items()
                       if not a.get("finished") or t - (a.get("mtime") or 0) < AGENT_SHOW_DONE}
            for aid in visible:
                a = ags[aid]
                parent = OWNER.get(a.get("tool_use")) or ("s:" + sid)
                if parent.startswith("a:") and parent[2:] not in visible:
                    parent = "s:" + sid
                if a.get("finished"):
                    ast = "done"
                elif st in ("paused", "closed"):
                    ast = "stopped"
                else:
                    ast = "working"
                agents.append({"id": "a:" + aid, "sid": sid, "parent": parent, "type": a.get("type"),
                               "desc": a.get("desc"), "status": ast, "mtime": a.get("mtime")})
            shown = display_name(sid, s)
            sessions.append({
                "id": "s:" + sid, "sid": sid, "name": s.get("name") or "", "title": TITLES.get(sid, ""),
                "display": shown, "subtitle": subtitle(sid, s, shown), "label": s.get("label") or "",
                "cwd": tilde(s["cwd"]), "folder": "f:" + fkey, "status": st, "owned": bool(o),
                "origin": s.get("origin") or "", "kind": e.get("kind") or s.get("kind") or "",
                "entrypoint": e.get("entrypoint") or s.get("entrypoint") or "", "live": bool(e) or bool(o),
                "agent": agent_of(s) or (s.get("agent") or "claude"),
                "agent_label": (AGENTS_CFG.get(agent_of(s)) or {}).get("label") or s.get("agent") or "Claude Code",
                "caps": session_caps(o, s),
                "pending": [{k: v for k, v in r.items() if k != "input" and not k.startswith("_")} for r in (o.pending.values() if o else [])],
                "errors": list(OWNED[sid].err)[-3:] if sid in OWNED else [],
                "hidden_agents": len(ags) - len(visible), "closed_at": s.get("closed_at"), "mode": s.get("mode"),
                "suggestion": o.suggestion if o else "",
                "model": s.get("model") or "", "effort": s.get("effort") or "",
                "parent": "s:" + s["parent"] if s.get("parent") in STATE["sessions"] else "",
                "can_takeover": bool(e) and not o and e.get("status") == "idle" and st != "closed"})
        places, seen = [], set()
        extra = [{"path": os.path.realpath(os.path.expanduser(str(x))), "name": ""} for x in CFG.get("extra_folders") or []]
        for p in [{"path": ROOT, "name": folder_name(ROOT)}] + extra + hub_projects() + [{"path": f["abs"], "name": f["name"]} for f in folders.values()]:
            if p["path"] in seen or not os.path.isdir(p["path"]):
                continue
            seen.add(p["path"])
            places.append({"path": p["path"], "label": "%s  ·  %s" % (p.get("name") or folder_name(p["path"]), tilde(p["path"]))})
        return {"boot": BOOT, "now": t, "folders": list(folders.values()), "closed_folders": shut, "sessions": sessions, "agents": agents,
                "usage": {"enabled": usage_enabled(), "data": USAGE["data"], "checked": USAGE["checked"], "error": USAGE["error"], "busy": USAGE["busy"]},
                "release": STATE.get("release"), "stats": {"cpu": STATS["cpu"], "rss_mb": STATS["rss_mb"],
                                                           "interval": FAST if CLIENTS else SLOW, "owned": len([1 for o in OWNED.values() if o.alive()])},
                "places": places, "agent_types": list(AGENT_TYPES), "cleanup_hour": CLEANUP_HOUR if CLEANUP_ON else None,
                "backends": [{"id": a["id"], "label": a["label"], "type": a["type"], "caps": a["caps"],
                            "models": [m for m in a.get("models") or [] if isinstance(m, dict) and m.get("value")]}
                           for a in AGENTS_CFG.values()],
                "default_agent": DEFAULT_AGENT, "can_pick_folder": can_pick_folder(), "user_name": USER_NAME,
                "defaults": user_defaults(), "board_defaults": STATE.get("defaults") or {}}


def events_after(last):
    with LOCK:
        if last <= 0 or last > SEQ:
            evs = [e for dq in NODE_EV.values() for e in dq]
            evs.sort(key=lambda e: e["seq"])
        else:
            evs = []
            for e in reversed(EVENTS):
                if e["seq"] <= last:
                    break
                evs.append(e)
            evs.reverse()
        return evs, SEQ


# ---------------------------------------------------------------- names

STOPWORDS = set("""a an the please can could would should you me i we my our to for of and or with in on at by
from this that these those it its is are be been was were do does did just also then now let lets let's help
hey hi ok okay so some any all into about up out make sure""".split())


def clean_label(v):
    v = re.sub(r"[\x00-\x1f\x7f]", " ", str(v or ""))
    return re.sub(r"\s+", " ", v).strip()[:80]


def slug_name(text):
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]*", text or "") if w.lower() not in STOPWORDS]
    name = " ".join(words[:5])[:48].strip()
    return (name[:1].upper() + name[1:]) if name else "New session"


DERIVED_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-[0-9a-f]{2}$")


def is_derived(s):
    """Claude Code's automatic names look like 'prog-lang-6e'; an AI title says more."""
    ns = s.get("name_source")
    return ns == "derived" or (not ns and bool(DERIVED_RE.match(s.get("name") or "")))


def display_name(sid, s):
    reg = s.get("name") if s.get("name") and not is_derived(s) else None
    for v in (s.get("label"), CUSTOM.get(sid), reg, s.get("auto_name"), TITLES.get(sid), s.get("name")):
        if v:
            return v
    return sid[:8]


def subtitle(sid, s, shown):
    for v in (TITLES.get(sid), s.get("name")):
        if v and v != shown:
            return v
    return tilde(s.get("cwd"))


def safe_control(o, request, timeout=20):
    try:
        return o.control(request, timeout)
    except Exception as e:
        print("%s failed: %r" % (request.get("subtype"), e), file=sys.stderr)
        return None


def auto_name(sid, o, prompt):
    """Give a board-started session a meaningful name from its first message (in the agent too, where it can)."""
    try:
        title = clean_label(o.make_title(prompt))
    except Exception as e:
        print("make_title failed: %r" % (e,), file=sys.stderr)
        title = ""
    title = title or slug_name(prompt)
    with LOCK:
        s = STATE["sessions"].get(sid)
        if s is None or s.get("label"):
            return
        s["auto_name"] = title
    try:
        o.rename(title)
    except Exception:
        pass
    save_state()
    changed()


# ---------------------------------------------------------------- actions

def get_session(body):
    sid = str(body.get("sid") or "")
    if not UUID_RE.match(sid) or sid not in STATE["sessions"]:
        raise ValueError("Unknown session.")
    return sid, STATE["sessions"][sid]


def clean_text(body, key="text", limit=20000):
    v = body.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError("Write something first.")
    if len(v) > limit:
        raise ValueError("That's too long.")
    return v.strip()


def act_new(body):
    cwd = os.path.realpath(os.path.expanduser(str(body.get("cwd") or ROOT)))
    if not os.path.isdir(cwd) or not in_root(cwd):
        raise ValueError("Pick a folder inside %s." % tilde(ROOT))
    agent = str(body.get("agent") or (STATE.get("defaults") or {}).get("agent") or DEFAULT_AGENT or "")
    if agent not in AGENTS_CFG:
        raise ValueError("No agent is set up to start. Add one to config.json (see docs/ADAPTERS.md).")
    caps = AGENTS_CFG[agent]["caps"]
    atts = body.get("attachments") or []
    if not isinstance(atts, list):
        raise ValueError("Bad attachments.")
    prompt = body.get("prompt") if isinstance(body.get("prompt"), str) else ""
    if not prompt.strip() and not atts:
        raise ValueError("Write something first.")
    if len(prompt) > 20000:
        raise ValueError("That's too long.")
    prompt = prompt.strip()
    bd = STATE.get("defaults") or {}
    mode = body.get("mode") if body.get("mode") in MODES else (bd.get("mode") if bd.get("mode") in MODES else "ask")
    name = body.get("name") or None
    if name and not NAME_RE.match(name):
        raise ValueError("Names can use letters, digits, spaces, dots and dashes.")
    model = body.get("model") if MODEL_RE.match(str(body.get("model") or "")) else None
    effort = body.get("effort") if body.get("effort") in EFFORTS else None
    if "model" not in body:
        model = bd.get("model") or None
    if "effort" not in body:
        effort = bd.get("effort") or None
    if not caps.get("effort"):
        effort = None
    if not caps.get("models") and AGENTS_CFG[agent]["type"] != "claude":
        model = None
    parent = str(body.get("parent") or "")
    if parent and not UUID_RE.match(parent):
        raise ValueError("Unknown parent session.")
    sid = str(uuid.uuid4())
    content = build_content(sid, prompt, atts, native_docs=AGENTS_CFG[agent]["type"] == "claude")  # checked before anything starts
    if folder_key(cwd) in STATE["folders"]:  # starting a session in a closed folder opens it
        folder_open(folder_key(cwd))
    with LOCK:  # the record comes first: the backend reports its own session id into it as soon as it has one
        STATE["sessions"][sid] = new_record(sid, cwd, name=name or "", origin="board", entrypoint="board",
                                            kind="board", mode=mode, model=model, effort=effort, parent=parent or None,
                                            agent=agent)
    try:
        o = make_owned(sid, cwd, agent, mode=mode, context=build_context(cwd, parent or None), name=name, model=model, effort=effort)
    except Exception:
        with LOCK:
            STATE["sessions"].pop(sid, None)
        raise
    with LOCK:
        OWNED[sid] = o
    save_state()
    o.send(content)
    if not name:
        about = prompt or "Look at these files: " + ", ".join(str((a or {}).get("name") or "file") for a in atts)
        threading.Thread(target=auto_name, args=(sid, o, about), daemon=True).start()
    return {"sid": sid}


def build_content(sid, text, atts, native_docs=True):
    """Images go to the agent directly, PDFs too where the agent takes them, text files inline, anything else is
    saved in the data folder and pointed to."""
    if not atts:
        return text
    blocks, total = [], 0
    for a in atts[:10]:
        if not isinstance(a, dict):
            raise ValueError("Bad attachment.")
        name = os.path.basename(str(a.get("name") or "file")).strip()[:120] or "file"
        mt = str(a.get("type") or "").lower()
        data = str(a.get("data") or "")
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception:
            raise ValueError("Couldn't read %s." % name)
        total += len(raw)
        if total > 30 * 1024 * 1024:
            raise ValueError("Attachments are limited to 30 MB per message.")
        if mt in IMAGE_TYPES:
            if len(raw) > 5 * 1024 * 1024:
                raise ValueError("%s is over 5 MB, which is too large to send as an image." % name)
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
        elif native_docs and (mt == "application/pdf" or name.lower().endswith(".pdf")):
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}, "title": name})
        else:
            txt = None
            if len(raw) <= 400000:
                try:
                    txt = raw.decode("utf-8")
                except UnicodeDecodeError:
                    txt = None
            if txt is not None and "\x00" not in txt:
                blocks.append({"type": "text", "text": '<attachment name="%s">\n%s\n</attachment>' % (name.replace('"', "'"), txt)})
            else:
                d = os.path.join(ATTACH_DIR, sid)
                os.makedirs(d, exist_ok=True)
                stem, ext = os.path.splitext(name)
                path, n = os.path.join(d, name), 1
                while os.path.exists(path):
                    n += 1
                    path = os.path.join(d, "%s-%d%s" % (stem, n, ext))
                with open(path, "wb") as f:
                    f.write(raw)
                blocks.append({"type": "text", "text": '<attachment name="%s" path="%s">%s attached this file. It is saved on this computer at %s; read it from there if you need it.</attachment>' % (name.replace('"', "'"), path, THE_USER.capitalize(), path)})
    if text:
        blocks.append({"type": "text", "text": text})
    return blocks


def act_send(body):
    sid, _ = get_session(body)
    atts = body.get("attachments") or []
    if not isinstance(atts, list):
        raise ValueError("Bad attachments.")
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    if not text.strip() and not atts:
        raise ValueError("Write something first.")
    if len(text) > 20000:
        raise ValueError("That's too long.")
    o = owned_alive(sid)
    if not o:
        raise Busy("Replies from the board work for sessions running on the board. Resume it here first.")
    o.send(build_content(sid, text.strip(), atts, native_docs=o.kind == "claude"), now=body.get("now") is True)
    return {"ok": True}


def act_agent(body):
    sid, _ = get_session(body)
    kind = body.get("type")
    if kind not in AGENT_TYPES:
        raise ValueError("Pick an agent type.")
    task = clean_text(body, "task", 4000)
    o = owned_alive(sid)
    if not o:
        raise Busy("Only a session running on the board can be asked to start an agent.")
    if not o.caps.get("subagents"):
        raise Busy("This agent has no sub-agent types to pick from. Ask it in the message instead.")
    o.send("Start a %s agent for this task, then tell me what it found: %s" % (kind, task))
    return {"ok": True}


def act_permission(body):
    sid, _ = get_session(body)
    o = owned_alive(sid)
    if not o:
        raise Busy("That session isn't running on the board any more.")
    o.answer(str(body.get("request_id") or ""), bool(body.get("allow")))
    return {"ok": True}


def act_interrupt(body):
    sid, _ = get_session(body)
    o = owned_alive(sid)
    if not o:
        raise Busy("That session isn't running on the board.")
    o.interrupt()
    return {"ok": True}


def act_resume(body):
    sid = str(body.get("sid") or "")
    if not UUID_RE.match(sid):
        raise ValueError("Unknown session.")
    if sid not in STATE["sessions"]:  # brought back from history
        hits = glob.glob(os.path.join(PROJ_DIR, "*", sid + ".jsonl")) + [p for p in [own_transcript(sid)] if os.path.exists(p)]
        if not hits:
            raise ValueError("That session's transcript is gone.")
        h = hist_scan_file(hits[0]) or {}
        cwd = h.get("cwd") if h.get("cwd") and os.path.isdir(h.get("cwd")) else ROOT
        with LOCK:
            STATE["sessions"][sid] = new_record(sid, cwd, origin="board", agent=h.get("agent") or "claude",
                                                agent_sid=h.get("agent_sid"))
            TRANSCRIPT[sid] = hits[0]
    s = STATE["sessions"][sid]
    s.pop("folder_paused", None)
    with LOCK:
        if owned_alive(sid) or sid in STARTING:
            return {"ok": True}
        if REG.get(sid):
            raise Busy("It's still open somewhere else. Close it there first, then resume it here.")
        STARTING.add(sid)
    try:
        return _resume(sid, s, body)
    finally:
        STARTING.discard(sid)


def _resume(sid, s, body):
    mode = body.get("mode") if body.get("mode") in MODES else (s.get("mode") or "ask")
    cwd = s["cwd"] if os.path.isdir(s["cwd"]) else ROOT
    agent = agent_of(s)
    if not agent:
        raise Busy("The agent for this session (%s) isn't set up on this computer." % (s.get("agent") or "?"))
    o = make_owned(sid, cwd, agent, resume=True, mode=mode, context=build_context(cwd), model=s.get("model"),
                   effort=s.get("effort"), agent_sid=s.get("agent_sid"))
    with LOCK:
        OWNED[sid] = o
        s.update(origin="board", closed_at=None, mode=mode)
    save_state()
    changed()
    return {"ok": True}


def open_terminal(cwd, cmd):
    """Open a terminal window running cmd in cwd. macOS Terminal, or the first terminal emulator found on Linux."""
    if sys.platform == "darwin":
        script = ['on run argv', 'tell application "Terminal"', 'activate',
                  'do script ("cd " & quoted form of (item 1 of argv) & " && " & (item 2 of argv))', 'end tell', 'end run']
        args = ["osascript"]
        for line in script:
            args += ["-e", line]
        subprocess.run(args + [cwd, cmd], capture_output=True, timeout=20)
        return
    for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("konsole", "-e"), ("xterm", "-e")):
        if shutil.which(term):
            subprocess.Popen([term, flag, "sh", "-c", "cd %s && %s; exec $SHELL" % (shlex.quote(cwd), cmd)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return
    raise Busy("No terminal program was found. Run this yourself: cd %s && %s" % (shlex.quote(cwd), cmd))


def act_navigate(body):
    sid, s = get_session(body)
    e = REG.get(sid)
    cwd = s["cwd"] if os.path.isdir(s["cwd"]) else ROOT
    o = owned_alive(sid)
    cfg = AGENTS_CFG.get(agent_of(s)) or {}
    if (s.get("agent") or "claude") != "claude" and not cfg:
        raise Busy("The agent for this session isn't set up on this computer.")
    if cfg.get("type") == "acp" or (o and o.kind != "claude"):
        tpl = cfg.get("resume_command")  # optional, e.g. "myagent --resume {session}"
        if not tpl:
            raise Busy("%s can't be reopened in a terminal from the board." % (cfg.get("label") or "This agent"))
        if o and (o.pending or o.status == "working"):
            raise Busy("It's working. Wait until it's idle, or stop it, then navigate.")
        if o:
            with LOCK:
                OWNED.pop(sid, None)
            o.close()
        open_terminal(cwd, tpl.replace("{session}", shlex.quote(str(s.get("agent_sid") or sid))))
        return {"where": "terminal"}
    if not CLAUDE:
        raise Busy("Claude Code isn't installed.")
    if o:
        if o.pending or o.status == "working":
            raise Busy("It's working. Wait until it's idle, or stop it, then navigate.")
        with LOCK:
            OWNED.pop(sid, None)
        o.close()
        open_terminal(cwd, "%s --resume %s" % (shlex.quote(CLAUDE), sid))
        return {"where": "terminal"}
    if e and e.get("kind") == "bg":
        open_terminal(cwd, "%s attach %s" % (shlex.quote(CLAUDE), sid))
        return {"where": "terminal"}
    ep = (e or {}).get("entrypoint") or s.get("entrypoint")
    if ep == "claude-vscode" and s.get("origin") != "board":
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, "vscode://anthropic.claude-code/open?session=" + sid], capture_output=True, timeout=15)
        return {"where": "vscode"}
    if e and sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'tell application "Terminal" to activate'], capture_output=True, timeout=15)
        return {"where": "terminal"}
    open_terminal(cwd, "%s --resume %s" % (shlex.quote(CLAUDE), sid))
    return {"where": "terminal"}


def act_close(body):
    sid, s = get_session(body)
    with LOCK:
        o = OWNED.pop(sid, None)
    if o and o.alive():
        threading.Thread(target=o.close, daemon=True).start()
    e = REG.get(sid)
    if e and e.get("kind") == "bg" and CLAUDE:
        subprocess.run([CLAUDE, "stop", sid], capture_output=True, timeout=30)
    with LOCK:
        s["closed_at"] = now()
    save_state()
    changed()
    return {"ok": True}


def act_reopen(body):
    sid, s = get_session(body)
    with LOCK:
        s["closed_at"] = None
    save_state()
    changed()
    return {"ok": True}


def folder_key(cwd):
    proj = project_for(cwd)
    return proj["path"] if proj else cwd


def folder_group(fkey):
    """The sessions a folder holds: the ones directly in it (not in a subfolder's own block), plus every
    sub-session split off them, wherever it runs. Closed sessions aren't part of it."""
    sess = STATE["sessions"]
    out = [sid for sid, s in sess.items() if not s.get("closed_at") and folder_key(s["cwd"]) == fkey]
    seen = set(out)
    for sid in out:  # out grows while we walk it: that's the descent through sub-sessions
        for cid, c in sess.items():
            if c.get("parent") == sid and cid not in seen and not c.get("closed_at"):
                seen.add(cid)
                out.append(cid)
    return out


def folder_arg(body):
    fkey = str(body.get("path") or "")
    with LOCK:
        if not any(folder_key(s["cwd"]) == fkey for s in STATE["sessions"].values()):
            raise ValueError("That folder has no sessions on the board.")
    return fkey


def act_folder_close(body):
    """Close a folder: its board sessions are paused (processes stopped, nothing closed or trashed), and are
    marked so opening the folder brings exactly those back. Sessions open elsewhere are left running."""
    fkey = folder_arg(body)
    paused = elsewhere = 0
    with LOCK:
        STATE["folders"][fkey] = {"closed_at": now()}
        sids = folder_group(fkey)
    for sid in sids:
        s = STATE["sessions"][sid]
        o = owned_alive(sid)
        if o:
            with LOCK:
                OWNED.pop(sid, None)
                STOPPING[sid] = o
                s["folder_paused"] = True
            try:
                if o.status in ("working", "waiting"):
                    o.interrupt()
            except Exception:
                pass
            threading.Thread(target=o.close, daemon=True).start()
            paused += 1
        elif REG.get(sid):
            elsewhere += 1
    save_state()
    changed()
    return {"ok": True, "paused": paused, "elsewhere": elsewhere}


def folder_open(fkey):
    """Open a folder: bring back the sessions its close paused, and their sub-sessions."""
    with LOCK:
        STATE["folders"].pop(fkey, None)
        sids = [sid for sid in folder_group(fkey) if STATE["sessions"][sid].get("folder_paused")]
    resumed = 0
    for sid in sids:
        old = STOPPING.pop(sid, None)
        if old:  # still shutting down from the close: wait for it, two live processes of one session would clash
            try:
                old.p.wait(timeout=12)
            except Exception:
                pass
        try:
            act_resume({"sid": sid})  # also clears the mark
            resumed += 1
        except (Busy, ValueError):
            STATE["sessions"][sid].pop("folder_paused", None)
    save_state()
    changed()
    return resumed


def act_folder_open(body):
    return {"ok": True, "resumed": folder_open(folder_arg(body))}


def act_rename(body):
    sid, s = get_session(body)
    name = clean_label(body.get("name"))
    with LOCK:
        s["label"] = name or None
    o = owned_alive(sid)
    if o and name:
        threading.Thread(target=o.rename, args=(name,), daemon=True).start()
    save_state()
    changed()
    return {"ok": True}


def act_takeover(body):
    """Move a session that is open elsewhere (VS Code, a terminal, the background) onto the board."""
    sid, s = get_session(body)
    if owned_alive(sid):
        return {"ok": True}
    e = REG.get(sid)
    if not e:
        return act_resume(body)
    if e.get("status") != "idle":
        raise Busy("It's busy right now. Wait until it's idle, then move it here.")
    pid = int(e.get("pid") or 0)
    if e.get("kind") == "bg" and CLAUDE:
        subprocess.run([CLAUDE, "stop", sid], capture_output=True, timeout=30)
    elif pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(60):
        if not pid or pid_gone(pid):
            break
        time.sleep(0.25)
    else:
        raise Busy("It didn't stop. Close it where it's open, then resume it here.")
    with LOCK:
        REG.pop(sid, None)
    return act_resume(body)


def act_commands(body):
    if not body.get("sid"):  # a composer that starts a new session
        return {"commands": GLOBAL_INIT["commands"], "models": GLOBAL_INIT["models"]}
    sid, _ = get_session(body)
    o = owned_alive(sid)
    if not o:
        return {"commands": []}
    for _ in range(40 if o.kind == "claude" else 0):  # Claude Code answers its initialize a moment after start
        if o.commands:
            break
        time.sleep(0.1)
    return {"commands": o.commands, "models": o.models}


FILE_CACHE = {}
SKIP_DIRS = {"node_modules", "Library", "Applications", "Movies", "Music", "Pictures", "Videos", "venv", ".venv",
             "__pycache__", "build", "dist", "target", "DerivedData", "snap"}


def list_files(cwd):
    c = FILE_CACHE.get(cwd)
    if c and now() - c[0] < 60:
        return c[1]
    files = []
    try:
        r = subprocess.run(["git", "-C", cwd, "ls-files", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, text=True, timeout=4)
        if r.returncode == 0:
            files = r.stdout.splitlines()[:20000]
    except Exception:
        pass
    if not files:  # not a git repo: walk breadth-first so nearby files come first
        t0, queue = now(), deque([("", 0)])
        while queue and len(files) < 8000 and now() - t0 < 0.8:
            rel, depth = queue.popleft()
            try:
                entries = sorted(os.scandir(os.path.join(cwd, rel)), key=lambda e: e.name.lower())
            except OSError:
                continue
            for e in entries:
                if e.name.startswith("."):
                    continue
                path = os.path.join(rel, e.name) if rel else e.name
                try:
                    isdir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if isdir:
                    if e.name in SKIP_DIRS:
                        continue
                    files.append(path + "/")
                    if depth < 5:
                        queue.append((path, depth + 1))
                else:
                    files.append(path)
    FILE_CACHE[cwd] = (now(), files)
    return files


def act_files(body):
    if body.get("sid"):
        sid, s = get_session(body)
        cwd = s["cwd"]
    else:  # a new session's folder
        cwd = os.path.realpath(os.path.expanduser(str(body.get("cwd") or "")))
        if not os.path.isdir(cwd) or not in_root(cwd):
            raise ValueError("Unknown folder.")
    q = str(body.get("q") or "").lower()[:120]
    scored = []
    for f in list_files(cwd):
        fl = f.lower()
        base = fl.rstrip("/").rsplit("/", 1)[-1]
        if not q:
            score = 3 - min(f.count("/"), 3)
        elif base.startswith(q):
            score = 100 - len(base) / 10
        elif q in base:
            score = 60 - len(base) / 10
        elif q in fl:
            score = 30 - len(fl) / 20
        else:
            continue
        scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    return {"files": [f for _, f in scored[:12]]}


HIST = {}  # transcript path -> what the history list needs


def hist_scan_file(p):
    """Head of a transcript for its folder and first prompt; titles from what was appended since the last look."""
    try:
        st = os.stat(p)
    except OSError:
        return None
    h = HIST.get(p)
    if h and h["mtime"] == st.st_mtime and h["size"] == st.st_size:
        return h
    if h is None or st.st_size < h["off"]:
        h = {"sid": os.path.basename(p)[:-len(".jsonl")], "cwd": None, "first": None, "ai": None, "custom": None, "off": 0,
             "agent": None, "agent_sid": None}
        try:
            with open(p, "rb") as f:
                for i, raw in enumerate(f):
                    if i > 400 or (h["cwd"] and h["first"]):
                        break
                    if len(raw) > 300000:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    h["cwd"] = h["cwd"] or d.get("cwd")
                    if d.get("type") == "sg-meta":
                        h["agent"], h["agent_sid"] = d.get("agent"), d.get("acpSessionId")
                    if not h["first"] and d.get("type") == "user" and not d.get("isMeta"):
                        t = prompt_text((d.get("message") or {}).get("content"))
                        if t:
                            h["first"] = t[:240]
        except OSError:
            return None
    try:
        with open(p, "rb") as f:
            f.seek(h["off"])
            while True:
                raw = f.readline()
                if not raw or not raw.endswith(b"\n"):
                    break
                h["off"] += len(raw)
                if b'"ai-title"' in raw or b'"custom-title"' in raw:
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    if d.get("type") == "ai-title" and d.get("aiTitle"):
                        h["ai"] = d["aiTitle"]
                    elif d.get("type") == "custom-title" and d.get("customTitle"):
                        h["custom"] = d["customTitle"]
    except OSError:
        return None
    h["mtime"], h["size"] = st.st_mtime, st.st_size
    HIST[p] = h
    return h


def all_transcripts():
    return glob.glob(os.path.join(PROJ_DIR, "*", "*.jsonl")) + glob.glob(os.path.join(OWN_TR_DIR, "*.jsonl"))


def act_history(body):
    items = []
    for p in all_transcripts():
        sid = os.path.basename(p)[:-len(".jsonl")]
        if not UUID_RE.match(sid):
            continue
        h = hist_scan_file(p)
        if not h or not h.get("first") or (h.get("cwd") or "").startswith(PROBE_DIR):
            continue
        cwd = h.get("cwd") or ROOT
        proj = project_for(cwd)
        with LOCK:
            s = STATE["sessions"].get(sid)
            status = session_status(sid, s) if s else ("live" if sid in REG else "")
            title = (s and s.get("label")) or h.get("custom") or h.get("ai") or h["first"].split("\n")[0][:90]
        items.append({"sid": sid, "title": title, "first": h["first"], "cwd": tilde(cwd),
                      "folder": (proj["name"] or proj["id"]) if proj else folder_name(cwd),
                      "mtime": h["mtime"], "tracked": bool(s), "status": status})
    items.sort(key=lambda x: -x["mtime"])
    return {"items": items[:400]}


STATS_FILE = os.path.join(DATA, "stats.json")
ACTIVITY = {"files": None, "archive": None}  # counts per transcript (by session id), and counts kept from deleted ones
STATS_LOCK = threading.Lock()


def stats_load():
    if ACTIVITY["files"] is None:
        try:
            with open(STATS_FILE) as f:
                d = json.load(f)
        except Exception:
            d = {}
        ACTIVITY["files"] = d.get("files") or {}
        ACTIVITY["archive"] = d.get("archive") or {"prompts": {}, "agents": {}, "folders": {}, "sessions": {}}


def stats_save():
    tmp = STATS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"files": ACTIVITY["files"], "archive": ACTIVITY["archive"]}, f)
    os.replace(tmp, STATS_FILE)


def stats_folder(cwd):
    cwd = cwd or ROOT
    proj = project_for(cwd)
    return (proj["name"] or proj["id"]) if proj else folder_name(cwd)


def stats_scan_file(p, sid):
    """Prompts (by local day and hour) and agents started (by day) in one transcript. True if it read anything new."""
    try:
        size = os.stat(p).st_size
    except OSError:
        return False
    h = ACTIVITY["files"].get(sid)
    if h is None or size < h["off"]:
        h = {"off": 0, "cwd": None, "prompts": {}, "agents": {}}
    ACTIVITY["files"][sid] = h
    if h["off"] == size:
        return False
    try:
        with open(p, "rb") as f:
            f.seek(h["off"])
            while True:
                raw = f.readline()
                if not raw or not raw.endswith(b"\n"):
                    break
                h["off"] += len(raw)
                user = b'"type":"user"' in raw
                agent = b'"type":"assistant"' in raw and (b'"name":"Agent"' in raw or b'"name":"Task"' in raw)
                if not (user or agent or not h["cwd"]) or len(raw) > 2000000:
                    continue
                try:
                    d = json.loads(raw)
                    day = dt.datetime.fromisoformat(d["timestamp"]).astimezone()
                except Exception:
                    continue
                h["cwd"] = h["cwd"] or d.get("cwd")
                key = day.strftime("%Y-%m-%d")
                if d.get("type") == "user" and not d.get("isMeta") and d.get("turnOrigin") != "task_notification" \
                        and d.get("promptSource") != "system":  # typed here, in VS Code or on the board (and briefs to sub-sessions)
                    t = prompt_text((d.get("message") or {}).get("content"))
                    if t and not t.startswith("This session is being continued"):
                        k = "%s %02d" % (key, day.hour)
                        h["prompts"][k] = h["prompts"].get(k, 0) + 1
                elif d.get("type") == "assistant":
                    n = sum(1 for b in (d.get("message") or {}).get("content") or []
                            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"))
                    if n:
                        h["agents"][key] = h["agents"].get(key, 0) + n
    except OSError:
        pass
    return True


def stats_archive(sid):
    """Its transcript is gone (closed, then cleaned up): keep its counts, but not which session they came from."""
    h = ACTIVITY["files"].pop(sid, None)
    if not h or (h["cwd"] or "").startswith(PROBE_DIR):
        return
    a, name, days = ACTIVITY["archive"], stats_folder(h["cwd"]), set()
    for k, n in h["prompts"].items():
        a["prompts"][k] = a["prompts"].get(k, 0) + n
        f = a["folders"].setdefault(k[:10], {})
        f[name] = f.get(name, 0) + n
        days.add(k[:10])
    for d in days:
        a["sessions"][d] = a["sessions"].get(d, 0) + 1
    for d, n in h["agents"].items():
        a["agents"][d] = a["agents"].get(d, 0) + n


def stats_keep(tp, sid):
    """Called just before the cleanup deletes a transcript, so its activity still counts."""
    try:
        with STATS_LOCK:
            stats_load()
            stats_scan_file(tp, sid)
            stats_archive(sid)
            stats_save()
    except Exception as e:
        print("stats keep failed:", repr(e), file=sys.stderr)


def act_stats(body):
    """Activity for Master: prompts, sessions and agents per day (14 days), prompts by folder (7 days) and by hour (30 days)."""
    today = dt.date.today()
    days = [(today - dt.timedelta(days=13 - i)).isoformat() for i in range(14)]
    week, month, keep = set(days[-7:]), (today - dt.timedelta(days=29)).isoformat(), (today - dt.timedelta(days=90)).isoformat()
    per_day = {d: {"d": d, "prompts": 0, "sessions": 0, "agents": 0} for d in days}
    folders, hours = {}, [0] * 24

    def add(k, n, name):
        d = k[:10]
        if d >= month:
            hours[int(k[11:])] += n
        if d in per_day:
            per_day[d]["prompts"] += n
        if d in week and name:
            folders[name] = folders.get(name, 0) + n

    with STATS_LOCK:
        stats_load()
        seen, changed = set(), False
        for p in all_transcripts():
            sid = os.path.basename(p)[:-len(".jsonl")]
            if UUID_RE.match(sid):
                seen.add(sid)
                changed = stats_scan_file(p, sid) or changed
        for sid in [x for x in ACTIVITY["files"] if x not in seen]:
            stats_archive(sid)
            changed = True
        a = ACTIVITY["archive"]
        for part in ("prompts", "agents", "folders", "sessions"):  # a few months is plenty
            old = [k for k in a[part] if k[:10] < keep]
            for k in old:
                del a[part][k]
            changed = changed or bool(old)
        if changed:
            stats_save()
        for h in ACTIVITY["files"].values():
            if (h["cwd"] or "").startswith(PROBE_DIR):
                continue
            name, active = stats_folder(h["cwd"]), set()
            for k, n in h["prompts"].items():
                add(k, n, name)
                active.add(k[:10])
            for d in active & set(per_day):
                per_day[d]["sessions"] += 1
            for d, n in h["agents"].items():
                if d in per_day:
                    per_day[d]["agents"] += n
        for k, n in a["prompts"].items():
            add(k, n, None)
        for d, fs in a["folders"].items():
            if d in week:
                for name, n in fs.items():
                    folders[name] = folders.get(name, 0) + n
        for d in per_day:
            per_day[d]["sessions"] += a["sessions"].get(d, 0)
            per_day[d]["agents"] += a["agents"].get(d, 0)
    return {"today": days[-1], "days": [per_day[d] for d in days], "hours": hours,
            "folders": sorted(({"name": k, "prompts": v} for k, v in folders.items()), key=lambda f: -f["prompts"])}


def act_open_folder(body):
    """Open a folder in the editor: VS Code if its `code` command is installed, else the system file manager."""
    path = os.path.realpath(os.path.expanduser(str(body.get("path") or "")))
    if not os.path.isdir(path) or not in_root(path):
        raise ValueError("That folder isn't available.")
    code = shutil.which("code")
    if code:
        subprocess.Popen([code, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    elif sys.platform == "darwin":
        subprocess.run(["open", path], capture_output=True, timeout=15)
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        raise Busy("No editor or file manager was found to open it with.")
    return {"ok": True}


PICKING = threading.Lock()


def act_pick_folder(body):
    """The system's own folder picker, so Master can start the first session in a folder the board hasn't seen yet."""
    if not can_pick_folder():
        raise Busy("This system has no folder picker the board can use. List the folder under \"extra_folders\" in config.json.")
    if not PICKING.acquire(blocking=False):
        raise Busy("The folder picker is already open.")
    try:
        if sys.platform == "darwin":
            cmd = ["osascript", "-e", "activate", "-e",
                   'POSIX path of (choose folder with prompt "Start a session in" default location (path to documents folder))']
        else:
            cmd = ["zenity", "--file-selection", "--directory", "--title=Start a session in"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"path": ""}
    finally:
        PICKING.release()
    if r.returncode != 0:
        if "-128" in r.stderr or sys.platform != "darwin":  # cancelled
            return {"path": ""}
        raise ValueError("Couldn't open the folder picker.")
    path = os.path.realpath(r.stdout.strip())
    if not os.path.isdir(path) or not in_root(path):
        raise ValueError("Pick a folder inside %s." % tilde(ROOT))
    return {"path": path, "name": folder_name(path), "tilde": tilde(path)}


def restart_owned(sid):
    """Restart a board session so new launch flags apply. A restart is a resume: nothing is lost."""
    o = owned_alive(sid)
    s = STATE["sessions"][sid]
    if o and (o.pending or o.status == "working"):
        raise Busy("It's working. The new setting is saved and applies when you change it again while it's idle.")
    if o:
        o.close()
    cwd = s["cwd"] if os.path.isdir(s["cwd"]) else ROOT
    n = make_owned(sid, cwd, agent_of(s), resume=True, mode=s.get("mode") or "ask", context=build_context(cwd),
                   model=s.get("model"), effort=s.get("effort"), agent_sid=s.get("agent_sid"))
    with LOCK:
        OWNED[sid] = n


def act_settings(body):
    """Change permission mode, model or effort of a board session, live where the agent allows it."""
    sid, s = get_session(body)
    o = owned_alive(sid)
    if not o:
        raise Busy("Mode, model and effort can be changed for sessions running on the board.")
    restart = False

    def apply(fn, value):
        nonlocal restart
        try:
            fn(value)
        except Busy:
            raise
        except Exception as e:
            if o.kind != "claude":
                raise Busy(str(e) or "The agent refused that.")
            print("setting failed: %r" % (e,), file=sys.stderr)
            restart = True  # Claude Code applies it on a resume

    if "mode" in body:
        mode = body.get("mode")
        if mode not in MODES:
            raise ValueError("Unknown mode.")
        apply(o.set_mode, mode)
        s["mode"] = mode
    if "model" in body:
        model = str(body.get("model") or "")
        if model and not MODEL_RE.match(model):
            raise ValueError("Unknown model.")
        apply(o.set_model, model)
        s["model"] = model or None
    if "effort" in body:
        effort = body.get("effort") or ""
        if effort and effort not in EFFORTS:
            raise ValueError("Unknown effort level.")
        apply(o.set_effort, effort)
        s["effort"] = effort or None
    save_state()
    if restart:
        restart_owned(sid)
    changed()
    return {"ok": True, "restarted": restart}


def act_defaults(body):
    """The board's defaults for new sessions (agent, mode, model, effort). The agents' own settings are not touched."""
    d = dict(STATE.get("defaults") or {})
    if "agent" in body:
        if body["agent"] not in AGENTS_CFG:
            raise ValueError("Unknown agent.")
        if d.get("agent") != body["agent"]:
            d["model"] = d["effort"] = ""  # models and effort levels belong to the agent
        d["agent"] = body["agent"]
    if "mode" in body:
        if body["mode"] not in MODES:
            raise ValueError("Unknown mode.")
        d["mode"] = body["mode"]
    if "model" in body:
        m = str(body.get("model") or "")
        if m and not MODEL_RE.match(m):
            raise ValueError("Unknown model.")
        d["model"] = m
    if "effort" in body:
        e = str(body.get("effort") or "")
        if e and e not in EFFORTS:
            raise ValueError("Unknown effort level.")
        d["effort"] = e
    with LOCK:
        STATE["defaults"] = d
    save_state()
    changed()
    return {"ok": True, "defaults": d}


def act_children(body):
    """A parent session's sub-sessions: status, pending permission prompts, and the last reply."""
    parent = str(body.get("parent") or "")
    if not UUID_RE.match(parent):
        raise ValueError("Unknown parent session.")
    out = []
    with LOCK:
        for sid, s in STATE["sessions"].items():
            if s.get("parent") != parent:
                continue
            last = next((e["text"] for e in reversed(NODE_EV.get("s:" + sid) or []) if e["kind"] == "reply"), "")
            o = owned_alive(sid)
            out.append({"sid": sid, "name": display_name(sid, s), "status": session_status(sid, s), "cwd": tilde(s["cwd"]),
                        "last_reply": last[-4000:],
                        "pending": [(r.get("tool") or "") + ": " + (r.get("detail") or "")[:200] for r in (o.pending.values() if o else [])]})
    return {"children": out}


def act_usage(body):
    threading.Thread(target=fetch_usage, daemon=True).start()
    return {"ok": True}


ACTIONS = {"new": act_new, "send": act_send, "agent": act_agent, "permission": act_permission,
           "interrupt": act_interrupt, "resume": act_resume, "navigate": act_navigate, "close": act_close,
           "reopen": act_reopen, "usage": act_usage, "rename": act_rename, "takeover": act_takeover,
           "commands": act_commands, "files": act_files, "settings": act_settings, "defaults": act_defaults, "children": act_children, "history": act_history, "open_folder": act_open_folder,
           "pick_folder": act_pick_folder, "stats": act_stats, "folder_close": act_folder_close, "folder_open": act_folder_open}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "SessionGraph/" + VERSION_STR
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _host_ok(self):
        return (self.headers.get("Host") or "") in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        if code >= 400:
            self.send_header("Connection", "close")  # never reuse a connection after an error
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "Open the board at http://127.0.0.1:%d/" % PORT})
        u = urlparse(self.path)
        if u.path in ("/", "/graph.html"):
            with open(PAGE_FILE, "rb") as f:
                html = f.read().replace(b"__GRAPH_TOKEN__", TOKEN.encode())
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/ping":
            return self._send(200, {"ok": True, "boot": BOOT})
        if u.path == "/api/events":
            return self._sse(u)
        return self._send(404, {"error": "Not found."})

    def _sse(self, u):
        global CLIENTS
        try:
            last = int(self.headers.get("Last-Event-ID") or (parse_qs(u.query).get("last") or ["0"])[0])
        except ValueError:
            last = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        with LOCK:
            CLIENTS += 1
        WAKE.set()
        seen = -1
        try:
            while True:
                with COND:
                    if VERSION == seen:
                        COND.wait(timeout=20)
                    v = VERSION
                if v == seen:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                graph = snapshot()
                evs, top = events_after(last)
                payload = json.dumps({"graph": graph, "events": evs}, ensure_ascii=False)
                self.wfile.write(("id: %d\ndata: %s\n\n" % (top, payload)).encode())
                self.wfile.flush()
                last, seen = top, v
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with LOCK:
                CLIENTS -= 1
            self.close_connection = True

    def do_POST(self):
        # Read the whole body first, even for a request we refuse; leftover bytes would corrupt the next
        # request on this keep-alive connection (Safari showed "Unsupported method ('{}GET')").
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        limit = 45 * 1024 * 1024 if urlparse(self.path).path in ("/api/send", "/api/new") else 200000
        if n < 0 or n > limit:
            self.close_connection = True
            return self._send(413, {"error": "Too large."})
        raw = self.rfile.read(n) if n else b""
        if not self._host_ok():
            return self._send(403, {"error": "Wrong host."})
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT):
            return self._send(403, {"error": "Wrong origin."})
        if not secrets.compare_digest(self.headers.get("X-Graph-Token") or "", TOKEN):
            return self._send(403, {"error": "The helper restarted. Reload the page."})
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self._send(400, {"error": "Bad request."})
        fn = ACTIONS.get(urlparse(self.path).path[len("/api/"):])
        if not fn:
            return self._send(404, {"error": "Unknown action."})
        try:
            return self._send(200, fn(body))
        except Busy as e:
            return self._send(409, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e) or "Bad request."})
        except Exception as e:
            print("action error:", repr(e), file=sys.stderr)
            return self._send(500, {"error": "Something failed in the helper: %s" % e})


# ---------------------------------------------------------------- main

class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind looks up this machine's name (socket.getfqdn). On macOS that can go out to the
        # local network over Bonjour, which raises the "find devices on your local network" prompt and stalls
        # startup. The board only listens on 127.0.0.1, so skip the lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = HOST, PORT


def scanner():
    while True:
        try:
            scan()
            maybe_cleanup()
            if update_stats() and CLIENTS:
                changed()
            if CLIENTS:
                if usage_enabled() and not USAGE["busy"] and now() - USAGE["checked"] > USAGE_EVERY:
                    threading.Thread(target=fetch_usage, daemon=True).start()
                if CFG.get("release_check") and CLAUDE and now() - ((STATE.get("release") or {}).get("checked") or 0) > RELEASE_EVERY:
                    threading.Thread(target=check_release, daemon=True).start()
        except Exception as e:
            print("scan error:", repr(e), file=sys.stderr)
        WAKE.wait(FAST if CLIENTS else SLOW)
        WAKE.clear()


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "serve":
        print(__doc__)
        return
    acp_backend.bind(sys.modules[__name__])
    load_state()
    catch_up()
    scan()
    threading.Thread(target=scanner, daemon=True).start()
    srv = LocalServer((HOST, PORT), Handler)
    try:
        with open(os.path.join(DATA, "server.pid"), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    print("Session Graph on http://%s:%d/  (agents: %s)" % (HOST, PORT, ", ".join(AGENTS_CFG) or "none set up"), flush=True)
    try:
        srv.serve_forever()
    finally:
        for o in list(OWNED.values()):
            o.close()


if __name__ == "__main__":
    main()
