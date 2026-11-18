#!/usr/bin/env python3
"""Look for personal or machine details in this folder before you share it.

    SCRUB_EXTRA="my name,my project" python3 tests/scrub_check.py [folder]

It builds its search terms at run time (your login name, home folder, host name, git name and e-mail, anything in
$SCRUB_EXTRA), so this file itself holds none of them. It also flags absolute home paths, e-mail addresses,
non-loopback IP addresses, long random-looking strings, and files that are runtime data (state, tokens, logs).
Exit status 1 if anything turns up.
"""
import getpass
import os
import re
import socket
import subprocess
import sys

root = os.path.realpath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
terms = set()


def add(t):
    t = (t or "").strip()
    if len(t) >= 3:
        terms.add(t.lower())


add(getpass.getuser())
add(os.path.expanduser("~"))
add(socket.gethostname())
add(socket.gethostname().split(".")[0])
for key in ("user.name", "user.email"):
    try:
        add(subprocess.run(["git", "config", "--global", key], capture_output=True, text=True, timeout=5).stdout)
    except Exception:
        pass
for t in os.environ.get("SCRUB_EXTRA", "").split(","):
    add(t)
for name in re.split(r"\s+", " ".join(t for t in list(terms))):
    pass

SKIP_DIRS = {".git", "__pycache__", "node_modules"}
BAD_FILES = re.compile(r"(^|/)(state\.json|stats\.json|config\.json|\.token|server\.pid|.*\.log|.*\.pyc|\.DS_Store)$")
PATTERNS = [
    ("home path", re.compile(r"/(Users|home)/[A-Za-z0-9._-]+")),
    ("e-mail address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("IP address", re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)\d{1,3}(?:\.\d{1,3}){3}\b")),
    ("long random string", re.compile(r"\b[A-Za-z0-9_-]{32,}\b")),
]
ALLOW = [re.compile(p) for p in (r"^\s*$",)]
hits = []
for dp, dns, fns in os.walk(root):
    dns[:] = [d for d in dns if d not in SKIP_DIRS]
    for fn in fns:
        path = os.path.join(dp, fn)
        rel = os.path.relpath(path, root)
        if BAD_FILES.search(rel):
            hits.append((rel, 0, "runtime file that shouldn't be shared", fn))
            continue
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(lines, 1):
            low = line.lower()
            for t in terms:
                if t in low:
                    hits.append((rel, n, "matches a personal term", line.strip()[:90]))
            for label, rx in PATTERNS:
                for m in rx.finditer(line):
                    s = m.group(0)
                    if label == "long random string" and (s.count("-") + s.count("_") > 3 or s.isalpha() or s.islower() and "_" in s):
                        continue  # identifiers and words, not secrets
                    if label == "e-mail address" and s.endswith(("@example.com", "@users.noreply.github.com")):
                        continue
                    hits.append((rel, n, label, s[:90]))

for h in hits:
    print("%s:%s  %s  %s" % h)
print("\n%d finding%s" % (len(hits), "" if len(hits) == 1 else "s") if hits else "clean: no personal or machine details found in %s" % root)
sys.exit(1 if hits else 0)
