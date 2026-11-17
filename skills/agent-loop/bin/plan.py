#!/usr/bin/env python3
"""An agent-team plan: check it, turn it into issues, load it into the run's state.

  plan.py --run <dir> check                    validate <dir>/plan.json; print waves, overlaps, suggested workers
  plan.py --run <dir> materialise              create an issue per task without one, labelled TEAM_LABEL (resumable); plan.json only
  plan.py --run <dir> load                     plan.json -> state.json (repo, mode, worker limit)
  plan.py --run <dir> pass <task-id>           the operator cleared the checkpoint after <task-id>

The supervisor runs check/materialise, and load once before it spawns the monitor. After that the
monitor is the only writer of state.json, so `load` and `pass` are the monitor's (PLAN_UPDATED,
CHECKPOINT_PASSED).

plan.json (written by the supervisor; schema in skills/agent-team/SKILL.md):
  {"goal": str, "repo": "group/project", "parent": iid|null, "workers": int,
   "mode": "review"|"merge", "hold": [globs], "shared": [globs],
   "usage_stop_5h": 1-100 (optional: the operator's 5h hard stop; usage.py --run reads it),
   "tasks": [{"id", "title", "model", "depends_on": [ids], "files": [globs],
              "checkpoint": false|true|"merge"|"dispatch", "brief": markdown, "iid": null,
              "branch": "existing branch to REATTACH to" (optional), "mr": iid (optional, needs branch),
              "base_branch": "a dependency's branch to stack on" (optional)}]}

A checkpoint (true = "merge") holds its dependents' merge until `pass`; "dispatch" also holds their dispatch.

`shared` names files every task may touch (CLAUDE.md, version and lock files): they never make two
parallel tasks clash; the monitor REBASEs whichever MR conflicts after the other merges.
"""
import fnmatch
import re
import sys
from common import *

REQUIRED = ["Goal", "Where", "What to do", "Check", "Done when"]
CHEAP_REQUIRED = REQUIRED + ["Edge cases", "Do not touch", "Known pitfalls"]
HAIKU_MAX_STEPS = 4      # longer haiku briefs come back half done
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def sections(brief):
    """{heading: body} for every `## heading` in a brief."""
    out, cur = {}, None
    for line in brief.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def steps(body):
    return sum(bool(re.match(r"^\s{0,3}(\d+[.)]|[-*])\s+\S", l)) for l in body.splitlines())


def reach(tasks):
    """{id: set of ids it (transitively) depends on}; raises on a cycle."""
    deps = {t["id"]: set(t.get("depends_on", [])) for t in tasks}
    memo, stack = {}, set()

    def walk(i):
        if i in memo:
            return memo[i]
        if i in stack:
            raise ValueError(f"dependency cycle through {i}")
        stack.add(i)
        r = set()
        for d in deps[i]:
            r |= {d} | walk(d)
        stack.discard(i)
        memo[i] = r
        return r

    for i in deps:
        walk(i)
    return memo


def waves(tasks, anc):
    level = {}
    for t in sorted(tasks, key=lambda t: len(anc[t["id"]])):
        level[t["id"]] = 1 + max((level[d] for d in t.get("depends_on", [])), default=0)
    out = {}
    for i, n in level.items():
        out.setdefault(n, []).append(i)
    return [sorted(out[n]) for n in sorted(out)]


WILD = re.compile(r"[*?\[]")


def may_share(x, y):
    """Could globs (or paths, or directories) x and y cover a common file? Errs towards yes."""
    x, y = x.rstrip("/"), y.rstrip("/")
    if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
        return True
    wx, wy = WILD.search(x), WILD.search(y)
    if not wx and not wy:
        return False
    if not wx or not wy:                       # one literal path: fnmatch is exact, or it's a dir above the glob
        lit, glob = (x, y) if not wx else (y, x)
        return fnmatch.fnmatch(lit, glob) or glob.startswith(lit + "/")
    px, py = x[:wx.start()], y[:wy.start()]
    if not (px.startswith(py) or py.startswith(px)):
        return False
    sx, sy = re.split(r"[*?\]]", x)[-1], re.split(r"[*?\]]", y)[-1]
    return sx.endswith(sy) or sy.endswith(sx)  # src/*.ts vs src/*.css can't meet


def overlap(a, b, shared=()):
    """Two file-glob lists may touch the same path. Entries covered by a `shared` glob don't count."""
    def own(files):                            # only literal paths are exempt: a glob entry may reach past the shared one
        return [f for f in files if WILD.search(f) or not any(f == g or fnmatch.fnmatch(f, g) for g in shared)]
    return sorted({f"{x} ~ {y}" if x != y else x for x in own(a) for y in own(b) if may_share(x, y)})


def max_parallel(ids, anc):
    """The most tasks that can run at once: the largest set with no dependency path between any two
    (Dilworth: tasks minus a maximum matching on the comparability graph)."""
    succ = {i: [j for j in ids if i in anc[j]] for i in ids}
    match = {}

    def augment(i, seen):
        for j in succ[i]:
            if j not in seen:
                seen.add(j)
                if j not in match or augment(match[j], seen):
                    match[j] = i
                    return True
        return False

    return len(ids) - sum(augment(i, set()) for i in ids)


def check(plan):
    """(errors, warnings, info) for a plan dict."""
    err, warn = [], []
    tasks = plan.get("tasks") or []
    if not plan.get("repo") or "/" not in plan["repo"]:
        err.append("repo must be group/project")
    if plan.get("mode", "review") not in ("review", "merge"):
        err.append("mode must be review or merge")
    if not tasks:
        err.append("no tasks")
    shared = plan.get("shared", [])
    if not isinstance(shared, list) or not all(isinstance(g, str) and g for g in shared):
        err.append("shared must be a list of file globs")
        shared = []
    ids = [t.get("id") for t in tasks]
    for i in ids:
        if not isinstance(i, str) or not ID_RE.match(i):
            err.append(f"bad task id {i!r} (lowercase, digits, '-', ≤ 31 chars)")
    if len(set(ids)) != len(ids):
        err.append("duplicate task ids")
    if err:
        return err, warn, {}
    for t in tasks:
        tid = t["id"]
        for d in t.get("depends_on", []):
            if d not in ids:
                err.append(f"{tid}: depends on unknown task {d}")
        if t.get("model") not in TIERS:
            err.append(f"{tid}: model must be one of {', '.join(TIERS)}")
        if not t.get("title"):
            err.append(f"{tid}: no title")
        if not t.get("files"):
            err.append(f"{tid}: list the files or globs it may touch")
        if t.get("mr") is not None and not (type(t["mr"]) is int and t["mr"] > 0 and t.get("branch")):
            err.append(f"{tid}: mr must be an MR iid and needs branch")
        if t.get("branch") is not None and not (isinstance(t["branch"], str) and t["branch"].strip()):
            err.append(f"{tid}: branch must be a branch name")
        if t.get("checkpoint") not in (None, False, True, "merge", "dispatch"):
            err.append(f"{tid}: checkpoint must be true, false, \"merge\" or \"dispatch\"")
        if t.get("base_branch") is not None:
            if not (isinstance(t["base_branch"], str) and t["base_branch"].strip()):
                err.append(f"{tid}: base_branch must be a branch name")
            elif t.get("branch"):
                err.append(f"{tid}: branch (REATTACH) or base_branch (stack), not both")
        sec = sections(t.get("brief", ""))
        need = CHEAP_REQUIRED if t.get("model") in ("haiku", "sonnet") else REQUIRED
        missing = [h for h in need if not sec.get(h)]
        if missing:
            err.append(f"{tid}: brief lacks ## {', ## '.join(missing)}")
        n = steps(sec.get("What to do", ""))
        if t.get("model") == "haiku" and n > HAIKU_MAX_STEPS:
            err.append(f"{tid}: {n} steps is too many for haiku (max {HAIKU_MAX_STEPS}); split it or use sonnet")
    if err:
        return err, warn, {}
    try:
        anc = reach(tasks)
    except ValueError as e:
        return [str(e)], warn, {}
    by = {t["id"]: t for t in tasks}
    clashes = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a in anc[b] or b in anc[a]:
                continue
            o = overlap(by[a]["files"], by[b]["files"], shared)
            if o:
                clashes.append(f"{a} and {b} can run at once but share {', '.join(o)}: add a dependency or split the files")
    err += clashes
    w = waves(tasks, anc)
    width = max_parallel(ids, anc)
    suggested = min(width, MAX_WORKERS)
    stop = plan.get("usage_stop_5h")
    if stop is not None and (isinstance(stop, bool) or not isinstance(stop, (int, float)) or not 1 <= stop <= 100):
        err.append("usage_stop_5h must be a number from 1 to 100")
    workers = plan.get("workers")
    if not isinstance(workers, int) or workers < 1:
        err.append("workers must be a positive integer")
    else:
        if workers > width:
            warn.append(f"workers={workers} but at most {width} tasks can ever run at once; extra workers would idle")
        if workers > MAX_WORKERS:
            warn.append(f"workers={workers} is over the default cap of {MAX_WORKERS}: shared rate limit, needs the operator's explicit OK")
    return err, warn, {"waves": w, "max_parallel": width, "suggested_workers": suggested,
                       "models": {m: sum(t["model"] == m for t in tasks) for m in TIERS},
                       "checkpoints": [t["id"] for t in tasks if t.get("checkpoint")]}


def plan_path():
    return os.path.join(STATE_DIR, "plan.json")


def load_plan():
    with open(plan_path()) as f:
        return json.load(f)


def save_plan(plan):
    tmp = plan_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(plan, f, indent=1)
    os.replace(tmp, plan_path())


def issue_body(plan, t, by):
    deps = [f"#{by[d]['iid']}" for d in t.get("depends_on", [])]
    foot = [f"Part of #{plan['parent']}." if plan.get("parent") else None,
            f"Depends on {', '.join(deps)}." if deps else None,
            f"Agent-team run `{os.path.basename(STATE_DIR)}`, task `{t['id']}`, worker model **{t['model']}**.",
            "Files: " + ", ".join(f"`{f}`" for f in t["files"]),
            (f"Continues existing branch `{t['branch']}`" + (f" (MR !{t['mr']})" if t.get("mr") else "")
             + ": REATTACH, don't open a new MR.") if t.get("branch") else None]
    return t["brief"].rstrip() + "\n\n---\n" + "\n".join(x for x in foot if x)


def materialise():
    plan = load_plan()
    err, _, info = check(plan)
    if err:
        raise SystemExit("plan fails check:\n  " + "\n  ".join(err))
    pid = api(f"projects/{enc(plan['repo'])}")["id"]
    by = {t["id"]: t for t in plan["tasks"]}
    for wave in info["waves"]:                     # dependencies first, so bodies can link their iids
        for tid in wave:
            t = by[tid]
            if t.get("iid"):
                continue
            made = api(f"projects/{pid}/issues", "POST",
                       {"title": t["title"], "description": issue_body(plan, t, by), "labels": TEAM_LABEL})
            t["iid"] = made["iid"]
            save_plan(plan)                        # after each one: a rerun never makes a duplicate
            print(f"{tid} -> #{made['iid']} {made['web_url']}")
            if TEAM_LABEL not in made.get("labels", []):   # label writes silently no-op below Reporter
                print(f"WARN #{made['iid']} has no {TEAM_LABEL} label: add it by hand")


def load():
    plan = load_plan()
    err, _, _ = check(plan)
    if err:
        raise SystemExit("plan fails check:\n  " + "\n  ".join(err))
    if any(not t.get("iid") for t in plan["tasks"]):
        raise SystemExit("materialise first: some tasks have no issue")
    st = load_state()
    p = api(f"projects/{enc(plan['repo'])}")
    st["repos"] = {plan["repo"]: {
        "id": p["id"], "name": p["path"], "mode": plan.get("mode", "review"), "hold": plan.get("hold", []),
        "default_branch": p["default_branch"], "web_url": p["web_url"]}}
    st["run"].update({"max_active": plan["workers"], "max_live": None, "started": st["run"].get("started") or now()})
    st["team"] = {"repo": plan["repo"], "parent": plan.get("parent"), "passed": st.get("team", {}).get("passed", [])}
    save_state(st)
    print(f"loaded: {len(plan['tasks'])} tasks, workers={plan['workers']}")


def human(err, warn, info):
    for e in err:
        print("ERROR", e)
    for w in warn:
        print("WARN ", w)
    if info:
        for n, w in enumerate(info["waves"], 1):
            print(f"wave {n}: {', '.join(w)}")
        print(f"max parallel {info['max_parallel']} · suggested workers {info['suggested_workers']} · "
              f"models {info['models']} · checkpoints {info['checkpoints'] or 'none'}")
    print("PLAN OK" if not err else "PLAN INVALID")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if x != "--json"]
    if a == ["check"]:
        e, w, i = check(load_plan())
        out({"errors": e, "warnings": w, **i}, lambda _: human(e, w, i))
        sys.exit(1 if e else 0)
    elif a == ["materialise"]:
        materialise()
    elif a == ["load"]:
        operator()
        load()
    elif len(a) == 2 and a[0] == "pass":
        st = load_state()
        if a[1] not in {t["id"] for t in load_plan()["tasks"]}:
            raise SystemExit(f"no task {a[1]}")
        st["team"]["passed"] = sorted(set(st["team"]["passed"]) | {a[1]})
        save_state(st)
        print("ok")
    else:
        raise SystemExit(__doc__)
