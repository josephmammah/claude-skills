#!/usr/bin/env python3
"""Small, atomic edits to state.json so the monitor never hand-edits JSON.

All take `--run <dir>` for an agent-team run (default ~/workspace/_agent-loop).

  state.py show
  state.py dispatch <key> repo=<path> branch=<b> worktree=<p> issues=<n,n> worker=<id> [model=<tier>] [task=<id>]
                                              (refuses a key that exists: change a live dispatch with `set`)
  state.py escalate <key> <reason>            move the dispatch to the next worker model; prints it
  state.py set <key> field=value ...          (value 'null' -> null, ints stay ints, lists as a,b;
                                               *sha and waiting_ci stay strings; a.b=v sets d[a][b])
  state.py heartbeat <key> [text]             stamps last_heartbeat (and last_activity if text says push/note);
                                               waiting_ci=<sha> in the text also sets status=AWAITING_CI
  state.py note <id> [<id>...]                record note ids this loop posted
  state.py event <text>                       append to the event log (kept to the last 200)
"""
import re
import sys
from common import *


def coerce(v):
    if v == "null": return None
    if v.isdigit(): return int(v)
    if v in ("true", "false"): return v == "true"
    if "," in v: return [coerce(x) for x in v.split(",")]
    return v


def text_key(k):
    return k.endswith("sha") or k == "waiting_ci"   # a short sha can be all digits


def kv(args):
    return {k: (None if v == "null" else v) if text_key(k.rsplit(".", 1)[-1]) else coerce(v)
            for k, v in (a.split("=", 1) for a in args)}


def apply(d, fields):
    """Write fields into d; a dotted key nests (park.note_id -> d["park"]["note_id"])."""
    for k, v in fields.items():
        *path, leaf = k.split(".")
        cur = d
        for p in path:
            if not isinstance(cur.get(p), dict):
                cur[p] = {}
            cur = cur[p]
        cur[leaf] = v


if __name__ == "__main__":
    st = load_state()
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "show":
        print(json.dumps(st, indent=1, sort_keys=True))
        sys.exit()
    if cmd == "dispatch":
        key, f = rest[0], kv(rest[1:])
        if key in st["dispatches"]:
            raise SystemExit(f"{key} already exists ({st['dispatches'][key]['status']}); use `state.py set {key} field=value`")
        issues = f.get("issues", [])
        f["issues"] = issues if isinstance(issues, list) else [issues]
        f.setdefault("model", "opus")
        if f["model"] not in TIERS:
            raise SystemExit(f"model must be one of {', '.join(TIERS)}")
        f["first_model"] = f["model"]
        st["dispatches"][key] = {"status": "IMPLEMENTING", "rounds": 0, "respawns": 0, "mr": None,
                                 "reviewed_sha": None, "last_heartbeat": now(), "last_activity": now(),
                                 "created": now()}
        apply(st["dispatches"][key], f)
    elif cmd == "escalate":
        d = st["dispatches"][rest[0]]
        cur = d.get("model", "opus")
        if cur not in TIERS or cur == TIERS[-1]:
            raise SystemExit(f"{rest[0]} is already on {cur}; nothing to escalate to, PARK it")
        d["model"] = TIERS[TIERS.index(cur) + 1]
        d.setdefault("escalations", []).append({"at": now(), "from": cur, "to": d["model"], "rounds": d.get("rounds", 0),
                                                "reason": " ".join(rest[1:])[:200]})
        d["rounds"] = 0                    # the new model gets a fresh round budget; the journal adds them back
        save_state(st)
        print(d["model"])
        sys.exit()
    elif cmd == "set":
        apply(st["dispatches"][rest[0]], kv(rest[1:]))
    elif cmd == "heartbeat":
        d = st["dispatches"][rest[0]]
        d["last_heartbeat"] = now()
        d["heartbeat_text"] = " ".join(rest[1:])[:200]
        if any(w in d["heartbeat_text"].lower() for w in ("push", "note", "reply", "mr ", "merge", "waiting_ci=")):
            d["last_activity"] = now()
        ci = re.search(r"\bwaiting_ci=(\S+)", " ".join(rest[1:]))
        if ci and d["status"] not in TERMINAL:
            d.update(status="AWAITING_CI", waiting_ci=ci.group(1))
    elif cmd == "note":
        st["my_note_ids"] = sorted(set(st["my_note_ids"]) | {int(x) for x in rest})
    elif cmd == "event":
        st.setdefault("events", []).append({"at": now(), "text": " ".join(rest)[:300]})
        st["events"] = st["events"][-200:]
    else:
        raise SystemExit(__doc__)
    save_state(st)
    print("ok")
