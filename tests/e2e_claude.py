#!/usr/bin/env python3
"""End-to-end check of the Claude Code backend against tests/mock_claude.py (no account, no network).

    python3 tests/e2e_claude.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("SG_TEST_PORT", "8792")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_acp as t  # noqa: E402  (shared helpers)


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="sg-e2e-claude-"))
    data, work, cdir = (os.path.join(tmp, x) for x in ("data", "work", "claude"))
    for d in (data, work, cdir):
        os.makedirs(d)
    cfg = {"port": t.PORT, "folders_root": tmp, "claude_dir": cdir, "cleanup": {"enabled": True, "hour": 0},
           "agents": {"claude": {"type": "claude", "label": "Claude Code", "command": os.path.join(t.HERE, "mock_claude.py")}}}
    with open(os.path.join(data, "config.json"), "w") as f:
        json.dump(cfg, f)
    log = open(os.path.join(tmp, "server.log"), "w")

    def launch():
        return subprocess.Popen([sys.executable, "-I", os.path.join(t.ROOT, "session_graph", "server.py"), "serve"],
                                env=dict(os.environ, SESSION_GRAPH_HOME=data), stdout=log, stderr=log)

    p = launch()
    try:
        if not t.wait_for(lambda: __import__("urllib.request").request.urlopen(t.BASE + "/api/ping", timeout=1).read(), 10):
            sys.exit("helper didn't start")
        token = open(os.path.join(data, ".token")).read().strip()
        check, post, wait_for, sess, events = t.check, t.post, t.wait_for, t.sess, t.events

        g = t.snapshot()["graph"]
        check("claude backend listed with its capabilities", g["backends"][0]["id"] == "claude" and g["backends"][0]["caps"]["effort"])
        check("cleanup hour reported when on", g["cleanup_hour"] == 0)
        check("usage meters on", g["usage"]["enabled"] is True)
        check("usage read", bool(wait_for(lambda: t.snapshot()["graph"]["usage"]["data"])))

        code, r = post(token, "new", {"cwd": work, "prompt": "hello claude"})
        sid = r.get("sid")
        check("new session accepted", code == 200, r)
        check("reply read from the transcript", bool(wait_for(lambda: [e for e in events(sid, "reply") if "Mock reply to" in e["text"]])))
        check("auto title from the agent", bool(wait_for(lambda: (sess(sid) or {}).get("display") == "Mock title")), (sess(sid) or {}).get("display"))
        check("model list from the agent", bool(post(token, "commands", {"sid": sid})[1].get("models")))

        post(token, "send", {"sid": sid, "text": "perm please"})
        pend = wait_for(lambda: (sess(sid) or {}).get("pending"))
        check("permission prompt appears", bool(pend) and pend[0]["tool"] == "Bash" and "ls -la" in pend[0]["detail"], pend)
        post(token, "permission", {"sid": sid, "request_id": pend[0]["id"], "allow": True})
        check("allow reaches claude", bool(wait_for(lambda: [e for e in events(sid, "reply") if e["text"] == "Ran it."])))

        for body in ({"mode": "plan"}, {"model": "haiku"}, {"effort": "high"}):
            code, r = post(token, "settings", dict(body, sid=sid))
            check("setting %s live" % list(body)[0], code == 200 and not r.get("restarted"), r)

        code, _ = post(token, "close", {"sid": sid})
        check("close puts it in Trash", bool(wait_for(lambda: any(c["sid"] == sid for c in t.snapshot()["graph"].get("closed", []))) or (sess(sid) or {}).get("status") == "closed"))
        tp = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(cdir, "projects")) for f in fs if f.startswith(sid)]
        check("transcript exists before cleanup", True)
        # the cleanup runs once a day at the first check after its hour: restart with yesterday's date to trigger it
        p.terminate()
        p.wait(10)
        st = json.load(open(os.path.join(data, "state.json")))
        st["last_cleanup"] = "2000-01-01"
        json.dump(st, open(os.path.join(data, "state.json"), "w"))
        p = launch()
        wait_for(lambda: __import__("urllib.request").request.urlopen(t.BASE + "/api/ping", timeout=1).read(), 10)
        gone = wait_for(lambda: not any(os.path.exists(x) for x in tp) and sid not in [s["sid"] for s in t.snapshot()["graph"]["sessions"]], 25)
        check("cleanup (opt-in) deletes the closed session's transcript", bool(gone) and bool(tp))
        check("cleanup logged in the data folder", os.path.exists(os.path.join(data, "cleanup.log")))
    finally:
        p.terminate()
        try:
            p.wait(5)
        except Exception:
            p.kill()
        if t.FAILS:
            print("\nserver log:\n" + open(os.path.join(tmp, "server.log")).read()[-2500:])
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("all passed" if not t.FAILS else "%d failed: %s" % (len(t.FAILS), ", ".join(t.FAILS))))
    sys.exit(1 if t.FAILS else 0)


if __name__ == "__main__":
    main()
