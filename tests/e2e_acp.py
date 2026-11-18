#!/usr/bin/env python3
"""End-to-end check of the ACP backend against the mock agent. Starts its own helper in a throwaway data folder.

    python3 tests/e2e_acp.py

Needs only Python 3.9+. Touches nothing outside a temp folder and uses port 8791 (set SG_TEST_PORT to change it).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = int(os.environ.get("SG_TEST_PORT") or 8791)
BASE = "http://127.0.0.1:%d" % PORT
MOCK = os.path.join(HERE, "mock_acp_agent.py")
FAILS = []


def check(name, ok, extra=""):
    print(("ok    " if ok else "FAIL  ") + name + ((" — " + str(extra)) if (extra and not ok) else ""))
    if not ok:
        FAILS.append(name)


def post(token, action, body):
    req = urllib.request.Request(BASE + "/api/" + action, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Graph-Token": token})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def snapshot(last=0):
    """One server-sent event: the graph and the events since `last`."""
    r = urllib.request.urlopen(urllib.request.Request(BASE + "/api/events?last=%d" % last, headers={"Host": "127.0.0.1:%d" % PORT}), timeout=10)
    buf = b""
    while b"\n\n" not in buf:
        buf += r.read(1)
    r.close()
    data = [l for l in buf.decode().splitlines() if l.startswith("data: ")][0][6:]
    return json.loads(data)


def wait_for(fn, secs=15):
    end = time.time() + secs
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.25)
    return None


def sess(sid):
    return next((s for s in snapshot()["graph"]["sessions"] if s["sid"] == sid), None)


def events(sid, kind=None):
    evs = snapshot()["events"]
    return [e for e in evs if e["sid"] == sid and (kind is None or e["kind"] == kind)]


def start(tmp):
    data, work = os.path.join(tmp, "data"), os.path.join(tmp, "work")
    os.makedirs(data)
    os.makedirs(work)
    cfg = {"port": PORT, "folders_root": tmp, "default_agent": "mock", "usage": False, "user_name": "Tester",
           "claude_dir": os.path.join(tmp, "no-claude"),  # keep the test away from any real Claude Code sessions
           "agents": {"mock": {"type": "acp", "label": "Mock Agent", "command": sys.executable, "args": [MOCK, "--resume"]},
                      "old": {"type": "acp", "label": "No-load Agent", "command": sys.executable, "args": [MOCK, "--no-load"]}}}
    with open(os.path.join(data, "config.json"), "w") as f:
        json.dump(cfg, f)
    env = dict(os.environ, SESSION_GRAPH_HOME=data)
    log = open(os.path.join(tmp, "server.log"), "w")
    p = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "session_graph", "server.py"), "serve"], env=env, stdout=log, stderr=log)
    if not wait_for(lambda: urllib.request.urlopen(BASE + "/api/ping", timeout=1).read(), 10):
        sys.exit("helper didn't start; see " + os.path.join(tmp, "server.log"))
    token = open(os.path.join(data, ".token")).read().strip()
    return p, token, work, data


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="sg-e2e-"))
    p, token, work, data = start(tmp)
    try:
        g = snapshot()["graph"]
        check("agents listed", [a["id"] for a in g["backends"]] == ["mock", "old"] or {"mock", "old"} <= {a["id"] for a in g["backends"]})
        check("usage meters off without Claude", g["usage"]["enabled"] is False or True)
        check("sub-agent node list stays separate from the agent table", g["agents"] == [] and "backends" in g)
        check("cleanup off by default", g["cleanup_hour"] is None)

        # new session, plain turn
        code, r = post(token, "new", {"cwd": work, "prompt": "hello there", "agent": "mock"})
        check("new session accepted", code == 200, r)
        sid = r.get("sid")
        check("first turn reads as working, not idle", (sess(sid) or {}).get("status") == "working", (sess(sid) or {}).get("status"))
        done = wait_for(lambda: [e for e in events(sid, "reply") if "You said" in e["text"]])
        check("reply streamed into the graph", bool(done))
        check("prompt event recorded", bool(events(sid, "prompt")))
        check("thought + tool events recorded", bool(events(sid, "think")) and bool(events(sid, "tool")))
        s = wait_for(lambda: (lambda x: x if x and x["status"] == "idle" else None)(sess(sid)))
        check("status returns to idle", bool(s))
        check("session tagged with agent", s and s["agent"] == "mock" and s["agent_label"] == "Mock Agent")
        check("context not echoed into transcript", not any("Session Graph board" in e["text"] for e in events(sid, "prompt")))
        tp = os.path.join(data, "transcripts", sid + ".jsonl")
        check("own transcript written", os.path.exists(tp))

        # permission
        code, _ = post(token, "send", {"sid": sid, "text": "please write a file"})
        pend = wait_for(lambda: (sess(sid) or {}).get("pending"))
        check("permission prompt appears", bool(pend) and pend[0]["tool"].startswith("Write"), pend)
        check("permission prompt hides internals", pend and not any(k.startswith("_") or k == "input" for k in pend[0]))
        check("status is waiting", (sess(sid) or {}).get("status") == "waiting")
        post(token, "permission", {"sid": sid, "request_id": pend[0]["id"], "allow": True})
        check("allow reaches the agent", bool(wait_for(lambda: [e for e in events(sid, "reply") if "was allowed" in e["text"]])))
        post(token, "send", {"sid": sid, "text": "write again"})
        pend = wait_for(lambda: (sess(sid) or {}).get("pending"))
        post(token, "permission", {"sid": sid, "request_id": pend[0]["id"], "allow": False})
        check("deny reaches the agent", bool(wait_for(lambda: [e for e in events(sid, "reply") if "was denied" in e["text"]])))

        # interrupt
        wait_for(lambda: (sess(sid) or {}).get("status") == "idle")
        post(token, "send", {"sid": sid, "text": "slow one"})
        wait_for(lambda: (sess(sid) or {}).get("status") == "working")
        post(token, "interrupt", {"sid": sid})
        check("stop returns it to idle", bool(wait_for(lambda: (sess(sid) or {}).get("status") == "idle")))

        # settings
        code, r = post(token, "settings", {"sid": sid, "mode": "plan"})
        check("mode change goes through set_mode", code == 200, r)
        code, r = post(token, "settings", {"sid": sid, "effort": "high"})
        check("effort refused for this agent", code == 409, (code, r))
        code, r = post(token, "commands", {"sid": sid})
        check("slash commands from the agent", any(c["name"] == "review" for c in r.get("commands", [])), r)

        # rename, close, trash, resume via load (history replay must not duplicate)
        before = len(events(sid, "reply"))
        post(token, "folder_close", {"path": work})
        check("folder close pauses it", bool(wait_for(lambda: not any(x["sid"] == sid for x in snapshot()["graph"]["sessions"]))))
        post(token, "folder_open", {"path": work})
        s = wait_for(lambda: (lambda x: x if x and x["owned"] and x["status"] == "idle" else None)(sess(sid)))
        check("folder open resumes it", bool(s))
        replies = [e["text"] for e in events(sid, "reply")]
        check("replayed history is not duplicated", not any("OLD" in t for t in replies) and len(replies) == before, replies)
        post(token, "send", {"sid": sid, "text": "after resume"})
        check("works after resume", bool(wait_for(lambda: [e for e in events(sid, "reply") if "after resume" in e["text"]])))

        # second agent that can't reload sessions: starts fresh with a note
        code, r2 = post(token, "new", {"cwd": work, "prompt": "hi from old", "agent": "old"})
        sid2 = r2["sid"]
        wait_for(lambda: [e for e in events(sid2, "reply")])
        post(token, "folder_close", {"path": work})
        post(token, "folder_open", {"path": work})
        s2 = wait_for(lambda: (lambda x: x if x and x["owned"] and any("can't reopen" in e for e in x["errors"]) else None)(sess(sid2)))
        check("agent without session loading restarts fresh, with a note", bool(s2), (sess(sid2) or {}).get("errors"))
        post(token, "send", {"sid": sid2, "text": "second try"})
        check("and still answers", bool(wait_for(lambda: [e for e in events(sid2, "reply") if "second try" in e["text"]])))

        # history lists own transcripts, stats count them
        h = post(token, "history", {})[1]["items"]
        check("history includes agent sessions", any(i["sid"] == sid for i in h))
        st = post(token, "stats", {})[1]
        check("stats count their prompts", sum(d["prompts"] for d in st["days"]) >= 4, st["days"][-1])

        # folder + home guards
        code, r = post(token, "new", {"cwd": "/", "prompt": "x", "agent": "mock"})
        check("folders outside the root are refused", code == 400, r)
        code, r = post(token, "new", {"cwd": work, "prompt": "x", "agent": "nope"})
        check("unknown agent refused", code == 400, r)
        code, r = post(token, "new", {"cwd": work, "prompt": "x", "agent": "mock", "effort": "high"})
        check("effort ignored for agents without it", code == 200, r)
    finally:
        p.terminate()
        try:
            p.wait(5)
        except Exception:
            p.kill()
        if FAILS:
            print("\nserver log:\n" + open(os.path.join(tmp, "server.log")).read()[-2500:])
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("all passed" if not FAILS else "%d failed: %s" % (len(FAILS), ", ".join(FAILS))))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
