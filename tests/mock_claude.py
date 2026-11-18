#!/usr/bin/env python3
"""A scripted stand-in for the `claude` command, for testing the board's Claude Code backend without an account.

It speaks the parts of Claude Code's stream-json mode the board uses, registers itself in
$CLAUDE_CONFIG_DIR/sessions and writes a transcript to $CLAUDE_CONFIG_DIR/projects, like the real one does.
A prompt containing "perm" makes it ask permission to run a command first.
"""
import json
import os
import re
import sys
import time
import uuid

cfg = os.environ["CLAUDE_CONFIG_DIR"]
args = sys.argv[1:]
if args and args[0] == "--version":
    print("0.0.0-mock")
    sys.exit(0)
PROBE = "--no-session-persistence" in args  # the board's usage check: no session, no files
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1] if "--resume" in args else "probe"
cwd = os.getcwd()
tdir = os.path.join(cfg, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
if not PROBE:
    os.makedirs(tdir, exist_ok=True)
    os.makedirs(os.path.join(cfg, "sessions"), exist_ok=True)
reg = os.path.join(cfg, "sessions", "%d.json" % os.getpid())
tp = os.path.join(tdir, sid + ".jsonl")


def ts():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def registry(status):
    if PROBE:
        return
    with open(reg, "w") as f:
        json.dump({"pid": os.getpid(), "sessionId": sid, "cwd": cwd, "status": status, "entrypoint": "sdk-cli",
                   "startedAt": int(time.time() * 1000), "name": ""}, f)


def line(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def transcript(obj):
    if PROBE:
        return
    obj.update(timestamp=ts(), cwd=cwd, sessionId=sid)
    with open(tp, "a") as f:
        f.write(json.dumps(obj) + "\n")


def ok(rid, response=None):
    line({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": response or {}}})


registry("idle")
try:
    for raw in sys.stdin:
        o = json.loads(raw)
        if o.get("type") == "control_request":
            sub = (o.get("request") or {}).get("subtype")
            if sub == "initialize":
                ok(o["request_id"], {"models": [{"value": "sonnet", "displayName": "Sonnet"}, {"value": "haiku", "displayName": "Haiku"}],
                                     "commands": [{"name": "help", "description": "Help"}]})
            elif sub == "generate_session_title":
                ok(o["request_id"], {"title": "Mock title"})
            elif sub == "get_usage":
                ok(o["request_id"], {"subscription_type": "mock", "rate_limits_available": True,
                                     "rate_limits": {"five_hour": {"utilization": 12, "resets_at": None}}})
            else:
                ok(o["request_id"])
        elif o.get("type") == "user":
            content = o["message"]["content"]
            text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if b.get("type") == "text")
            registry("busy")
            transcript({"type": "user", "message": {"role": "user", "content": text}, "uuid": str(uuid.uuid4())})
            if "perm" in text:
                line({"type": "control_request", "request_id": "perm1", "request": {
                    "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls -la"}, "display_name": "Bash"}})
                answer = json.loads(sys.stdin.readline())
                allowed = (answer.get("response", {}).get("response") or {}).get("behavior") == "allow"
                reply = "Ran it." if allowed else "Not allowed."
            else:
                reply = "Mock reply to: " + text
            transcript({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}})
            line({"type": "result", "subtype": "success", "is_error": False, "result": reply})
            registry("idle")
finally:
    try:
        os.remove(reg)
    except OSError:
        pass
