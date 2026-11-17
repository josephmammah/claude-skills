"""Shared helpers for the agent-loop scripts. Stdlib only; glab does the HTTP."""
import fnmatch, json, os, subprocess, sys, urllib.parse
from datetime import datetime, timezone

def _run_dir():
    """`--run <dir>` (an agent-team run) wins over $AGENT_LOOP_DIR; it is stripped from argv."""
    if "--run" in sys.argv:
        i = sys.argv.index("--run")
        if i + 1 >= len(sys.argv):
            raise SystemExit("--run needs a directory")
        d = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return os.path.expanduser(d)
    return os.path.expanduser(os.environ.get("AGENT_LOOP_DIR", "~/workspace/_agent-loop"))


STATE_DIR = _run_dir()
STATE = os.path.join(STATE_DIR, "state.json")
TEAM_ROOT = os.path.expanduser(os.environ.get("AGENT_TEAM_DIR", "~/workspace/_agent-team"))
JOURNAL = os.path.join(TEAM_ROOT, "journal.jsonl")
TIERS = ["haiku", "sonnet", "opus"]                       # worker models, cheapest first
LOOP_BUSY = ("IMPLEMENTING", "AWAITING_CI")               # the loop's 1-busy rule (PROTOCOL)
TEAM_BUSY = ("IMPLEMENTING", "CHANGES_REQUESTED")         # workers mid-turn; resuming a waiting one takes a slot too (PROTOCOL)
TERMINAL = ("MERGED", "RETIRED")
MAX_WORKERS = 4                                           # default cap; the operator can raise it per run
ENV = dict(os.environ, GLAB_CHECK_UPDATE="false")
WORKSPACE = os.path.expanduser(os.environ.get("AGENT_LOOP_WORKSPACE", "~/workspace"))  # where <repo-name> clones live


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


TEAM_LABEL = "agent-team-task"  # marks a team run's task issues; unscoped so an agent::* label can't evict it


def api(path, method="GET", fields=None):
    cmd = ["glab", "api", path]
    if method != "GET":
        cmd += ["-X", method]
    for k, v in (fields or {}).items():
        cmd += ["--raw-field", f"{k}={v}"]  # --field reads @file and expands :branch
    out = subprocess.run(cmd, capture_output=True, text=True, env=ENV)
    if out.returncode != 0:
        raise SystemExit(f"glab api {method} {path} failed: {out.stderr.strip()[:400]}")
    return json.loads(out.stdout) if out.stdout.strip() else None


_self = []


def self_account():
    """The GitLab account both agents post as: $AGENT_LOOP_ACCOUNT, else glab's logged-in user."""
    if not _self:
        _self.append(os.environ.get("AGENT_LOOP_ACCOUNT") or api("user")["username"])
    return _self[0]



def operator():
    """The operator's GitLab username ($AGENT_LOOP_OPERATOR); it must not be the agents' own account."""
    op = os.environ.get("AGENT_LOOP_OPERATOR", "")
    if not op:
        raise SystemExit("set AGENT_LOOP_OPERATOR to your GitLab username")
    if op == self_account():
        raise SystemExit(f"AGENT_LOOP_OPERATOR={op} is also the agents' account: give the agents their own "
                         "GitLab account (or set AGENT_LOOP_ACCOUNT to it)")
    return op


def paged(path):
    res, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        chunk = api(f"{path}{sep}per_page=100&page={page}")
        res += chunk
        if len(chunk) < 100:
            return res
        page += 1


def enc(project_path):
    return urllib.parse.quote(project_path, safe="")


def load_state():
    if not os.path.exists(STATE):
        return {"run": {}, "repos": {}, "dispatches": {}, "my_note_ids": [], "events": []}
    with open(STATE) as f:
        return json.load(f)


def read_plan():
    """The team run's plan.json, or None outside a team run."""
    path = os.path.join(STATE_DIR, "plan.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def checkpoint_hold(plan, passed, task_id):
    """Direct dependencies of task_id that are unpassed checkpoints: its MR may not merge or park for merge yet."""
    by = {t["id"]: t for t in (plan or {}).get("tasks", [])}
    return [d for d in by.get(task_id, {}).get("depends_on", []) if by[d].get("checkpoint") and d not in passed]


def limits(st):
    """(max busy workers, max live dispatches or None). The loop's defaults are 1 and 2."""
    run = st.get("run", {})
    return run.get("max_active", 1), run.get("max_live", 2)


def slots(st):
    active, live = limits(st)
    ds = [d for d in st["dispatches"].values() if d["status"] not in TERMINAL]
    busy_states = TEAM_BUSY if st.get("team") else LOOP_BUSY
    busy = sum(d["status"] in busy_states for d in ds)
    free = active - busy
    if live is not None:
        free = min(free, live - len(ds))
    return {"busy": busy, "max_active": active, "live": len(ds), "max_live": live, "free": max(free, 0),
            "waiting_ci": sum(d["status"] == "AWAITING_CI" for d in ds)}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def hold_hit(paths, globs):
    """Return the changed paths matching any hold glob (empty = no hold)."""
    return sorted({p for p in paths for g in globs if fnmatch.fnmatch(p, g)})


def out(obj, human=None):
    if "--json" in sys.argv or human is None:
        print(json.dumps(obj, indent=1))
    else:
        human(obj)
