#!/usr/bin/env python3
"""A tiny, scripted Agent Client Protocol agent for testing the board without any real agent or account.

    prompt contains "write"   -> asks permission before a (pretend) file write
    prompt contains "slow"    -> works for 30 s unless cancelled
    anything else             -> a thought, a tool call, and a reply that echoes the prompt

Options:  --no-load   don't advertise loadSession    --resume   advertise sessionCapabilities.resume
"""
import json
import sys
import threading
import time
import uuid

LOAD = "--no-load" not in sys.argv
RESUME = "--resume" in sys.argv
OUT = sys.stdout
LOCK = threading.Lock()
SESSIONS = {}
WAIT = {}   # request id -> (event, box)
CANCEL = {}


def send(obj):
    with LOCK:
        OUT.write(json.dumps(obj) + "\n")
        OUT.flush()


def update(sid, u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def text(t):
    return {"type": "text", "text": t}


def ask(sid, params):
    rid = "perm-" + uuid.uuid4().hex[:6]
    ev, box = threading.Event(), {}
    WAIT[rid] = (ev, box)
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": dict(params, sessionId=sid)})
    ev.wait(60)
    return (box.get("result") or {}).get("outcome") or {"outcome": "cancelled"}


def turn(rid, sid, prompt):
    said = " ".join(b.get("text", "") for b in prompt if b.get("type") == "text")
    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": text("thinking about it")})
    time.sleep(0.2)
    if "slow" in said:
        for _ in range(60):
            if CANCEL.pop(sid, False):
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
                return
            time.sleep(0.5)
    if "write" in said:
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "tc1", "title": "Write notes.txt", "kind": "edit",
                     "status": "pending", "rawInput": {"file_path": "notes.txt"}, "locations": [{"path": "notes.txt"}]})
        out = ask(sid, {"toolCall": {"toolCallId": "tc1", "title": "Write notes.txt", "kind": "edit",
                                     "rawInput": {"file_path": "notes.txt"}},
                        "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                                    {"optionId": "no", "name": "Reject", "kind": "reject_once"}]})
        if out.get("outcome") == "cancelled":
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
            return
        done = "allowed" if out.get("optionId") == "yes" else "denied"
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "tc1", "status": "completed" if done == "allowed" else "failed"})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": text("The write was %s." % done)})
    else:
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "tc0", "title": "Run ls", "kind": "execute",
                     "status": "pending", "rawInput": {"command": "ls"}})
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "tc0", "status": "completed"})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": text("You said: ")})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": text(said.split("---")[-1].strip()[:200])})
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def handle(o):
    m, p, rid = o.get("method"), o.get("params") or {}, o.get("id")
    if m is None and rid in WAIT:
        ev, box = WAIT.pop(rid)
        box["result"] = o.get("result")
        ev.set()
    elif m == "initialize":
        caps = {"loadSession": LOAD, "promptCapabilities": {"image": True}}
        if RESUME:
            caps["sessionCapabilities"] = {"resume": {}}
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": caps, "authMethods": []}})
    elif m == "session/new":
        sid = "sess_" + uuid.uuid4().hex[:10]
        SESSIONS[sid] = p.get("cwd")
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid, "modes": {"currentModeId": "default", "availableModes": [
            {"id": "default", "name": "Default"}, {"id": "plan", "name": "Plan"}, {"id": "yolo", "name": "Auto-approve"}]}}})
        update(sid, {"sessionUpdate": "available_commands_update",
                     "availableCommands": [{"name": "review", "description": "Review changes", "input": {"hint": "path"}}]})
    elif m in ("session/load", "session/resume"):
        sid = p.get("sessionId")
        SESSIONS[sid] = p.get("cwd")
        if m == "session/load":  # replays history, which the board must ignore
            update(sid, {"sessionUpdate": "user_message_chunk", "content": text("OLD PROMPT")})
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": text("OLD REPLY")})
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif m == "session/set_mode":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif m == "session/prompt":
        threading.Thread(target=turn, args=(rid, p["sessionId"], p.get("prompt") or []), daemon=True).start()
    elif m == "session/cancel":
        CANCEL[p.get("sessionId")] = True
        for rid2, (ev, box) in list(WAIT.items()):
            pass
    elif rid is not None and m:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method " + m}})


for line in sys.stdin:
    try:
        handle(json.loads(line))
    except Exception as e:
        print("mock agent error:", repr(e), file=sys.stderr, flush=True)
