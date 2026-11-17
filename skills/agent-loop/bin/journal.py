#!/usr/bin/env python3
"""The worker journal: what went wrong and what each run cost, across every loop and team run.

  journal.py add key=<dispatch> class=<task class> cause=<brief|stack|complexity|tool|env> what="..." [change="..."]
  journal.py outcome <key>                 (with --run <dir> for a team run) one line per finished dispatch
  journal.py stats [days]                  runs per first model, rounds, escalations, deaths; recurring classes

One JSON line per entry in ~/workspace/_agent-team/journal.jsonl. A class that recurs is the
signal to change skills/agent-team/BRIEF.md (a mandatory brief section) or add a check.
"""
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from common import *

CAUSES = ("brief", "stack", "complexity", "tool", "env")


def append(entry):
    os.makedirs(TEAM_ROOT, exist_ok=True)
    with open(JOURNAL, "a") as f:
        f.write(json.dumps({"at": now(), **entry}, sort_keys=True) + "\n")


def read(days=None):
    if not os.path.exists(JOURNAL):
        return []
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ""
    rows = []
    with open(JOURNAL) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("at", "") >= cut:
                rows.append(r)
    return rows


def outcome(st, key):
    d = st["dispatches"][key]
    return {"kind": "outcome", "run": os.path.basename(STATE_DIR), "key": key, "repo": d["repo"],
            "task": d.get("task"), "status": d["status"], "first_model": d.get("first_model", d.get("model")),
            "final_model": d.get("model"), "rounds": d.get("rounds", 0) + sum(e.get("rounds", 0) for e in d.get("escalations", [])),
            "respawns": d.get("respawns", 0),
            "escalations": len(d.get("escalations", [])), "created": d.get("created")}


def stats(rows):
    outs = [r for r in rows if r.get("kind") == "outcome"]
    fails = [r for r in rows if r.get("kind") == "failure"]
    by = defaultdict(list)
    for r in outs:
        by[r.get("first_model")].append(r)
    lines = []
    for m in TIERS:
        rs = by.get(m, [])
        if not rs:
            continue
        merged = sum(r["status"] == "MERGED" for r in rs)
        clean = sum(r["status"] == "MERGED" and r["rounds"] == 0 and r["escalations"] == 0 for r in rs)
        lines.append(f"{m:7} runs={len(rs)} merged={merged} first-time-clean={clean} "
                     f"rounds={sum(r['rounds'] for r in rs)} escalated={sum(r['escalations'] > 0 for r in rs)} "
                     f"respawns={sum(r['respawns'] for r in rs)}")
    cls = Counter(r.get("class") for r in fails)
    cause = Counter(r.get("cause") for r in fails)
    lines.append(f"failures={len(fails)} by cause: " + (", ".join(f"{k}={v}" for k, v in cause.most_common()) or "none"))
    for c, n in cls.most_common():
        flag = "  <- recurring: add it to BRIEF.md's task-class table or a check" if n >= 2 else ""
        lines.append(f"  {n} × {c}{flag}")
    return lines


if __name__ == "__main__":
    a = [x for x in sys.argv[1:]]
    if a[:1] == ["add"]:
        f = dict(x.split("=", 1) for x in a[1:])
        if f.get("cause") not in CAUSES or not f.get("class") or not f.get("what") or not f.get("key"):
            raise SystemExit(__doc__)
        append({"kind": "failure", "run": os.path.basename(STATE_DIR), **f})
        print("ok")
    elif a[:1] == ["outcome"] and len(a) == 2:
        append(outcome(load_state(), a[1]))
        print("ok")
    elif a[:1] == ["stats"]:
        print("\n".join(stats(read(int(a[1]) if len(a) > 1 else None))))
    else:
        raise SystemExit(__doc__)
