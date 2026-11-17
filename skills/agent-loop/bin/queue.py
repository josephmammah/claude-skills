#!/usr/bin/env python3
"""List dispatchable issues across the configured repos, in dispatch order.

Usage: queue.py [--run <dir>] [--json]            read repos + label (or the team plan) from state.json
       queue.py [--run <dir>] --init <spec>...    resolve repo specs into state.json `repos` (see SKILL.md for the grammar)

Loop: dispatchable = open, carries the queue label, no `blocked` or `agent-team-task` label (a team run owns it), not assigned to a human,
and not already in a live dispatch. Order: weight ascending (unset last), then iid.
Team (state.json has `team`): the plan's tasks in plan order; a task waits until every task it
depends on is merged (or its issue closed), except a `base_branch` task, which may start once the
dependency owning that branch has an MR. An unpassed checkpoint on a dependency holds the task's
merge (`merge_hold`), and its dispatch too when the checkpoint is "dispatch".
The last line (or `slots` in --json) says how many workers may be dispatched now.
"""
import sys
from common import *


def parse_spec(spec):
    parts = spec.split(":")
    path, mode = parts[0], (parts[1] if len(parts) > 1 else "review")
    if mode not in ("merge", "review"):
        raise SystemExit(f"{spec}: mode must be merge|review")
    hold = []
    for p in parts[2:]:
        if p.startswith("hold="):
            hold += [g for g in p[5:].split(",") if g]
        else:
            raise SystemExit(f"{spec}: unknown option {p}")
    return path, mode, hold


def init(specs):
    st = load_state()
    label = "agent-queue"
    repos = {}
    for s in specs:
        if s.startswith("label="):
            label = s[6:]
            continue
        path, mode, hold = parse_spec(s)
        p = api(f"projects/{enc(path)}")
        repos[path] = {"id": p["id"], "name": p["path"], "mode": mode, "hold": hold,
                       "default_branch": p["default_branch"], "web_url": p["web_url"]}
    st["repos"] = repos
    st["run"] = {"label": label, "specs": specs, "started": st.get("run", {}).get("started") or now()}
    save_state(st)
    return st


def dispatchable(st):
    label = st["run"].get("label", "agent-queue")
    live = {(d["repo"], i) for d in st["dispatches"].values()
            if d["status"] not in ("MERGED", "RETIRED") for i in d["issues"]}  # dropped repos just never match
    rows = []
    for path, r in st["repos"].items():
        for i in paged(f"projects/{r['id']}/issues?state=opened&labels={label}"):
            humans = [a["username"] for a in i.get("assignees", []) if a["username"] != self_account()]
            skip = ("blocked" in i["labels"] and "blocked") or (TEAM_LABEL in i["labels"] and "team") \
                or (humans and f"assigned:{','.join(humans)}") or ((path, i["iid"]) in live and "live")
            rows.append({"repo": path, "iid": i["iid"], "title": i["title"], "weight": i.get("weight"),
                         "labels": i["labels"], "milestone": (i.get("milestone") or {}).get("title"),
                         "web_url": i["web_url"], "skip": skip or None})
    rows.sort(key=lambda x: (x["skip"] is not None, x["weight"] is None, x["weight"] or 0, x["repo"], x["iid"]))
    return rows


def team_rows(st, plan, issues):
    """Plan tasks in order. `issues` = {iid: issue}; pure so the tests need no GitLab."""
    repo = st["team"]["repo"]
    by_iid = {}
    for d in st["dispatches"].values():
        for i in d["issues"]:
            by_iid[i] = d
    def done(t):
        d = by_iid.get(t.get("iid"))
        return (d is not None and d["status"] == "MERGED") or issues.get(t.get("iid"), {}).get("state") == "closed"
    def stacked_on(t, dep):
        b = by_iid.get(by_id[dep].get("iid"))
        return bool(t.get("base_branch")) and b is not None and b.get("branch") == t["base_branch"] \
            and b.get("mr") and b["status"] not in TERMINAL
    by_id = {t["id"]: t for t in plan["tasks"]}
    rows = []
    for t in plan["tasks"]:
        iid, d = t.get("iid"), by_iid.get(t.get("iid"))
        stacked = [x for x in t.get("depends_on", []) if not done(by_id[x]) and stacked_on(t, x)]
        waiting = [x for x in t.get("depends_on", []) if not done(by_id[x]) and x not in stacked]
        gated = [x for x in t.get("depends_on", []) if by_id[x].get("checkpoint") == "dispatch"
                 and x not in st["team"]["passed"]]
        skip = ((not iid) and "not-materialised") or (done(t) and "done") \
            or (d is not None and d["status"] not in TERMINAL and "live") \
            or (waiting and "waiting:" + ",".join(waiting)) or (gated and "checkpoint:" + ",".join(gated))
        rows.append({"repo": repo, "iid": iid, "task": t["id"], "title": t["title"], "model": t["model"],
                     "branch": t.get("branch"), "mr": t.get("mr"), "base_branch": t["base_branch"] if stacked else None,
                     "merge_hold": checkpoint_hold(plan, st["team"]["passed"], t["id"]),
                     "weight": None, "labels": [], "milestone": None,
                     "web_url": issues.get(iid, {}).get("web_url"), "skip": skip or None})
    return rows


def team_dispatchable(st):
    with open(os.path.join(STATE_DIR, "plan.json")) as f:
        plan = json.load(f)
    pid = st["repos"][st["team"]["repo"]]["id"]
    iids = [t["iid"] for t in plan["tasks"] if t.get("iid")]
    q = "&".join(f"iids[]={i}" for i in iids)
    issues = {i["iid"]: i for i in paged(f"projects/{pid}/issues?{q}")} if iids else {}
    return team_rows(st, plan, issues)


def human(rows):
    if not rows:
        print("QUEUE EMPTY")
    for r in rows:
        tag = f"skip={r['skip']}" if r["skip"] else "DISPATCHABLE"
        task = f" [{r['task']} {r['model']}]" if r.get("task") else ""
        if r.get("branch"):
            task += f" REATTACH={r['branch']}" + (f"!{r['mr']}" if r.get("mr") else "")
        if r.get("base_branch"):
            task += f" BASE={r['base_branch']}"
        if r.get("merge_hold"):
            task += f" merge_hold={','.join(r['merge_hold'])}"
        print(f"{tag:22} {r['repo']}#{r['iid']!s:<4} w={r['weight']}{task} | {r['title'][:70]}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--json"]
    if args and args[0] == "--init":
        operator()
        st = init(args[1:])
        print(json.dumps(st["repos"], indent=1))
    else:
        st = load_state()
        rows = team_dispatchable(st) if st.get("team") else dispatchable(st)
        sl = slots(st)
        if "--json" in sys.argv:
            print(json.dumps({"rows": rows, "slots": sl}, indent=1))
        else:
            human(rows)
            print(f"SLOTS free={sl['free']} busy={sl['busy']}/{sl['max_active']} live={sl['live']}/{sl['max_live'] or '-'} "
                  f"waiting_ci={sl['waiting_ci']}")
