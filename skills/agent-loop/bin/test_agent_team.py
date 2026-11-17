"""Tests for the agent-team additions: plan checks, the team queue, slots, escalation, the journal."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.TemporaryDirectory()
os.environ["AGENT_LOOP_DIR"] = os.path.join(TMP.name, "run")
os.environ["AGENT_TEAM_DIR"] = TMP.name
os.environ["AGENT_LOOP_ACCOUNT"] = "bot"
os.environ["AGENT_LOOP_OPERATOR"] = "op"
sys.path.insert(0, HERE)
import common  # noqa: E402
import plan  # noqa: E402
import journal  # noqa: E402
import mrstate  # noqa: E402
from unittest import mock  # noqa: E402

import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("teamqueue", os.path.join(HERE, "queue.py"))  # not stdlib queue
teamqueue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(teamqueue)


def tearDownModule():
    TMP.cleanup()

FULL = """## Goal
g
## Where
w
## What to do
1. a
2. b
## Edge cases
e
## Do not touch
d
## Check
npm test
## Done when
green
## Known pitfalls
p
"""


def task(i, model="haiku", deps=(), files=None, **kw):
    return {"id": i, "title": f"T {i}", "model": model, "depends_on": list(deps),
            "files": files or [f"src/{i}.ts"], "brief": FULL, "iid": None, **kw}


def mkplan(tasks, workers=2):
    return {"goal": "x", "repo": "group/app", "parent": 5, "workers": workers, "tasks": tasks}


class PlanCheck(unittest.TestCase):
    def test_valid_plan_waves_and_suggestion(self):
        e, w, i = plan.check(mkplan([task("a"), task("b"), task("c", deps=["a", "b"])]))
        self.assertEqual(e, [])
        self.assertEqual(i["waves"], [["a", "b"], ["c"]])
        self.assertEqual(i["max_parallel"], 2)
        self.assertEqual(i["suggested_workers"], 2)

    def test_cycle(self):
        e, _, _ = plan.check(mkplan([task("a", deps=["b"]), task("b", deps=["a"])]))
        self.assertTrue(any("cycle" in x for x in e), e)

    def test_unknown_dep_and_bad_model(self):
        e, _, _ = plan.check(mkplan([task("a", deps=["zz"], model="gpt")]))
        self.assertTrue(any("unknown task zz" in x for x in e), e)
        self.assertTrue(any("model must be" in x for x in e), e)

    def test_parallel_tasks_sharing_files_is_an_error(self):
        e, _, _ = plan.check(mkplan([task("a", files=["src/x.ts"]), task("b", files=["src/*.ts"])]))
        self.assertTrue(any("a and b can run at once" in x for x in e), e)

    def test_shared_files_do_not_clash(self):
        p = mkplan([task("a", files=["CLAUDE.md", "src/a.ts"]), task("b", files=["CLAUDE.md", "src/b.ts"])])
        e, _, i = plan.check(p)
        self.assertTrue(any("share CLAUDE.md" in x for x in e), e)
        p["shared"] = ["CLAUDE.md", "*/package.json"]
        e, _, i = plan.check(p)
        self.assertEqual(e, [])
        self.assertEqual(i["max_parallel"], 2)

    def test_shared_does_not_hide_real_overlap(self):
        p = mkplan([task("a", files=["CLAUDE.md", "src/x.ts"]), task("b", files=["CLAUDE.md", "src/x.ts"])])
        p["shared"] = ["CLAUDE.md"]
        e, _, _ = plan.check(p)
        self.assertTrue(any("share src/x.ts" in x for x in e), e)

    def test_glob_entry_is_not_exempted_by_a_shared_glob(self):
        p = mkplan([task("a", files=["src/*.ts"]), task("b", files=["src/yy.ts"])])
        p["shared"] = ["src/?.ts"]                 # fnmatch('src/*.ts', 'src/?.ts') is True
        e, _, _ = plan.check(p)
        self.assertTrue(any("a and b can run at once" in x for x in e), e)

    def test_shared_must_be_globs(self):
        p = mkplan([task("a")])
        p["shared"] = "CLAUDE.md"
        e, _, _ = plan.check(p)
        self.assertTrue(any("shared must be" in x for x in e), e)

    def test_reattach_fields(self):
        self.assertEqual(plan.check(mkplan([task("a", branch="feat/x", mr=43)]))[0], [])
        e, _, _ = plan.check(mkplan([task("a", mr=43)]))
        self.assertTrue(any("needs branch" in x for x in e), e)
        for bad in ("!43", True, 0, -1):
            e, _, _ = plan.check(mkplan([task("a", branch="feat/x", mr=bad)]))
            self.assertTrue(any("mr must be" in x for x in e), (bad, e))

    def test_issue_body_names_the_branch_to_reattach(self):
        t = task("a", branch="feat/x", mr=43)
        body = plan.issue_body(mkplan([t]), t, {"a": t})
        self.assertIn("Continues existing branch `feat/x` (MR !43): REATTACH", body)
        self.assertNotIn("REATTACH", plan.issue_body(mkplan([task("b")]), task("b"), {}))

    def test_materialise_labels_every_task_issue(self):
        posts = []

        def api(path, method="GET", fields=None):
            if method == "GET":
                return {"id": 7}
            posts.append(fields)
            return {"iid": len(posts), "web_url": "u", "labels": [plan.TEAM_LABEL] if len(posts) == 1 else []}
        p = mkplan([task("a"), task("b", deps=["a"])], 1)
        out = []
        with mock.patch.object(plan, "api", api), mock.patch.object(plan, "load_plan", lambda: p), \
                mock.patch.object(plan, "save_plan", lambda _: None), mock.patch("builtins.print", out.append):
            plan.materialise()
        self.assertEqual([f["labels"] for f in posts], ["agent-team-task"] * 2)
        self.assertEqual([x for x in out if x.startswith("WARN")], ["WARN #2 has no agent-team-task label: add it by hand"])

    def test_loop_queue_skips_team_tasks(self):
        st = {"run": {"label": "agent-queue"}, "dispatches": {}, "repos": {"x/y": {"id": 1}}}
        issues = [{"iid": n, "title": "t", "labels": labels, "web_url": "u", "assignees": []}
                  for n, labels in ((1, ["agent-queue"]), (2, ["agent-queue", "agent-team-task"]))]
        with mock.patch.object(teamqueue, "paged", lambda _: issues):
            rows = {r["iid"]: r["skip"] for r in teamqueue.dispatchable(st)}
        self.assertEqual(rows, {1: None, 2: "team"})

    def test_sequential_tasks_may_share_files(self):
        e, _, _ = plan.check(mkplan([task("a", files=["src/x.ts"]), task("b", deps=["a"], files=["src/x.ts"])], 1))
        self.assertEqual(e, [])

    def test_transitive_dependency_counts_as_sequential(self):
        tasks = [task("a", files=["f"]), task("b", deps=["a"]), task("c", deps=["b"], files=["f"])]
        e, _, _ = plan.check(mkplan(tasks, 1))
        self.assertEqual(e, [])

    def test_haiku_needs_full_brief_opus_does_not(self):
        thin = "## Goal\ng\n## Where\nw\n## What to do\n1. a\n## Check\nc\n## Done when\nd\n"
        e, _, _ = plan.check(mkplan([task("a", brief=thin)]))
        self.assertTrue(any("Known pitfalls" in x for x in e), e)
        e, _, _ = plan.check(mkplan([task("a", model="opus", brief=thin)]))
        self.assertEqual(e, [])

    def test_haiku_step_cap(self):
        many = FULL.replace("1. a\n2. b", "\n".join(f"{n}. s" for n in range(1, 7)))
        e, _, _ = plan.check(mkplan([task("a", brief=many)]))
        self.assertTrue(any("too many for haiku" in x for x in e), e)
        e, _, _ = plan.check(mkplan([task("a", model="sonnet", brief=many)]))
        self.assertEqual(e, [])

    def test_workers_beyond_parallelism_warns(self):
        e, w, _ = plan.check(mkplan([task("a"), task("b", deps=["a"])], workers=3))
        self.assertEqual(e, [])
        self.assertTrue(any("idle" in x for x in w), w)

    def test_workers_over_cap_warns(self):
        tasks = [task(f"t{n}") for n in range(6)]
        e, w, i = plan.check(mkplan(tasks, workers=6))
        self.assertEqual(e, [])
        self.assertEqual(i["suggested_workers"], common.MAX_WORKERS)
        self.assertTrue(any("cap" in x for x in w), w)

    def test_usage_stop_5h(self):
        for ok in (None, 80, 99.5):
            p = mkplan([task("a")])
            if ok is not None:
                p["usage_stop_5h"] = ok
            self.assertEqual(plan.check(p)[0], [], ok)
        for bad in (0, 101, "80", True):
            p = mkplan([task("a")])
            p["usage_stop_5h"] = bad
            self.assertTrue(any("usage_stop_5h" in x for x in plan.check(p)[0]), bad)

    def test_checkpoint_and_base_branch_fields(self):
        for ok in (True, False, "merge", "dispatch"):
            self.assertEqual(plan.check(mkplan([task("a", checkpoint=ok)]))[0], [], ok)
        e, _, _ = plan.check(mkplan([task("a", checkpoint="later")]))
        self.assertTrue(any("checkpoint must be" in x for x in e), e)
        self.assertEqual(plan.check(mkplan([task("a"), task("b", deps=["a"], base_branch="agent/1-a")]))[0], [])
        for bad in ("", 3):
            e, _, _ = plan.check(mkplan([task("a", base_branch=bad)]))
            self.assertTrue(any("base_branch must be" in x for x in e), (bad, e))
        e, _, _ = plan.check(mkplan([task("a", base_branch="x", branch="y")]))
        self.assertTrue(any("not both" in x for x in e), e)

    def test_mode(self):
        p = mkplan([task("a")])
        p["mode"] = "yolo"
        e, _, _ = plan.check(p)
        self.assertTrue(any("mode must be" in x for x in e), e)

    def test_overlap_beyond_fnmatch(self):
        cases = [(["src/*.ts"], ["src/a*"], True), (["src"], ["src/a.ts"], True), (["src/"], ["src/*.ts"], True),
                 (["src/*.ts"], ["src/*.css"], False), (["src/a.ts"], ["src/b*"], False),
                 (["app/*"], ["server/*"], False), (["src/a.ts"], ["src/a.ts"], True)]
        for a, b, clash in cases:
            with self.subTest(a=a, b=b):
                e, _, _ = plan.check(mkplan([task("x", files=a), task("y", files=b)]))
                self.assertEqual(any("can run at once" in m for m in e), clash, e)

    def test_max_parallel_is_the_largest_independent_set(self):
        # a->b, a->c, d, e->f: waves are [a,d,e],[b,c,f] but b, c, d, f can all run at once.
        tasks = [task("a"), task("b", deps=["a"]), task("c", deps=["a"]), task("d"), task("e"), task("f", deps=["e"])]
        _, _, i = plan.check(mkplan(tasks, 4))
        self.assertEqual(i["max_parallel"], 4)

    def test_idle_and_cap_warnings_both_show(self):
        e, w, _ = plan.check(mkplan([task("a"), task("b", deps=["a"])], workers=6))
        self.assertEqual(e, [])
        self.assertTrue(any("idle" in x for x in w) and any("cap" in x for x in w), w)

    def test_bad_ids(self):
        e, _, _ = plan.check(mkplan([task("A b"), task("A b")]))
        self.assertTrue(any("bad task id" in x for x in e))
        self.assertTrue(any("duplicate" in x for x in e))


def state(dispatches=None, passed=(), max_active=2):
    return {"run": {"max_active": max_active, "max_live": None}, "repos": {}, "my_note_ids": [], "events": [],
            "dispatches": dispatches or {}, "team": {"repo": "group/app", "parent": 5, "passed": list(passed)}}


class TeamQueue(unittest.TestCase):
    def setUp(self):
        self.p = mkplan([task("a", iid=10), task("b", iid=11, checkpoint=True), task("c", deps=["a", "b"], iid=12)])

    def rows(self, st, issues=None):
        return {r["task"]: r["skip"] for r in teamqueue.team_rows(st, self.p, issues or {})}

    def test_initial(self):
        self.assertEqual(self.rows(state()), {"a": None, "b": None, "c": "waiting:a,b"})

    def test_reattach_task_row_carries_branch_and_mr(self):
        p = mkplan([task("a", iid=10, branch="feat/x", mr=43)])
        r = teamqueue.team_rows(state(), p, {})[0]
        self.assertEqual((r["branch"], r["mr"]), ("feat/x", 43))
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            teamqueue.human([r])
        self.assertIn("REATTACH=feat/x!43", buf.getvalue())

    def test_live_and_merged(self):
        st = state({"x": {"status": "IN_REVIEW", "issues": [10]}, "y": {"status": "MERGED", "issues": [11]}})
        r = self.rows(st)
        self.assertEqual(r["a"], "live")
        self.assertEqual(r["b"], "done")
        self.assertEqual(r["c"], "waiting:a")

    def test_checkpoint_gates_merge_not_dispatch_by_default(self):
        st = state({"x": {"status": "MERGED", "issues": [10]}, "y": {"status": "MERGED", "issues": [11]}})
        r = {x["task"]: x for x in teamqueue.team_rows(st, self.p, {})}
        self.assertIsNone(r["c"]["skip"])
        self.assertEqual(r["c"]["merge_hold"], ["b"])
        st["team"]["passed"] = ["b"]
        self.assertEqual({x["task"]: x for x in teamqueue.team_rows(st, self.p, {})}["c"]["merge_hold"], [])

    def test_dispatch_checkpoint_gates_dependents_until_passed(self):
        self.p["tasks"][1]["checkpoint"] = "dispatch"
        st = state({"x": {"status": "MERGED", "issues": [10]}, "y": {"status": "MERGED", "issues": [11]}})
        self.assertEqual(self.rows(st)["c"], "checkpoint:b")
        st["team"]["passed"] = ["b"]
        self.assertIsNone(self.rows(st)["c"])

    def test_stacked_task_dispatches_once_its_base_has_an_mr(self):
        self.p["tasks"][2]["base_branch"] = "agent/11-b"
        x = {"status": "MERGED", "issues": [10]}
        st = state({"x": x, "y": {"status": "IMPLEMENTING", "issues": [11], "branch": "agent/11-b", "mr": None}})
        self.assertEqual(self.rows(st)["c"], "waiting:b")
        st["dispatches"]["y"].update(status="PARKED", mr=7)
        row = {r["task"]: r for r in teamqueue.team_rows(st, self.p, {})}["c"]
        self.assertEqual((row["skip"], row["base_branch"], row["merge_hold"]), (None, "agent/11-b", ["b"]))
        st["dispatches"]["x"]["status"] = "IN_REVIEW"     # only the base dependency may be unmerged
        self.assertEqual(self.rows(st)["c"], "waiting:a")
        st["dispatches"]["x"]["status"] = st["dispatches"]["y"]["status"] = "MERGED"
        row = {r["task"]: r for r in teamqueue.team_rows(st, self.p, {})}["c"]
        self.assertEqual((row["skip"], row["base_branch"]), (None, None))   # base merged: branch from default

    def test_closed_issue_counts_as_done(self):
        r = self.rows(state(passed=["b"]), {10: {"state": "closed"}, 11: {"state": "closed"}})
        self.assertIsNone(r["c"])

    def test_unmaterialised(self):
        self.p["tasks"][0]["iid"] = None
        self.assertEqual(self.rows(state())["a"], "not-materialised")


class Slots(unittest.TestCase):
    def test_loop_defaults(self):
        st = {"run": {}, "dispatches": {"a": {"status": "IN_REVIEW"}}}
        self.assertEqual(common.slots(st)["free"], 1)
        st["dispatches"]["a"]["status"] = "CHANGES_REQUESTED"     # the loop doesn't count review rounds as busy
        self.assertEqual(common.slots(st)["free"], 1)
        st["dispatches"]["b"] = {"status": "IMPLEMENTING"}
        self.assertEqual(common.slots(st)["free"], 0)

    def test_team_counts_only_busy_workers(self):
        st = state({"a": {"status": "IMPLEMENTING"}, "b": {"status": "CHANGES_REQUESTED"},
                    "c": {"status": "IN_REVIEW"}, "d": {"status": "PARKED"}, "e": {"status": "MERGED"}}, max_active=3)
        s = common.slots(st)
        self.assertEqual((s["busy"], s["free"]), (2, 1))

    def test_team_waiting_and_cleared_dispatches_free_their_slot(self):
        st = state({"a": {"status": "AWAITING_CI"}, "b": {"status": "CLEARED"}, "c": {"status": "IMPLEMENTING"}})
        s = common.slots(st)
        self.assertEqual((s["busy"], s["free"], s["live"], s["waiting_ci"]), (1, 1, 3, 1))


def run(*args):
    return subprocess.run([sys.executable, os.path.join(HERE, args[0]), *args[1:]],
                          capture_output=True, text=True, env=os.environ)


class StateAndJournal(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP.name)
        os.environ["AGENT_LOOP_DIR"] = os.path.join(self.dir, "loop")

    def test_dispatch_escalate_ladder(self):
        rund = os.path.join(self.dir, "teamrun")
        r = run("state.py", "--run", rund, "dispatch", "k", "repo=x/y", "branch=b", "worktree=w",
                "issues=1", "worker=w1", "model=haiku")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(rund, "state.json")), "--run must pick the state dir")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "loop", "state.json")))
        run("state.py", "--run", rund, "set", "k", "rounds=2")
        self.assertEqual(run("state.py", "--run", rund, "escalate", "k", "round 2").stdout.strip(), "sonnet")
        self.assertEqual(run("state.py", "--run", rund, "escalate", "k", "dead").stdout.strip(), "opus")
        self.assertNotEqual(run("state.py", "--run", rund, "escalate", "k", "again").returncode, 0)
        with open(os.path.join(rund, "state.json")) as f:
            d = json.load(f)["dispatches"]["k"]
        self.assertEqual((d["first_model"], d["model"], len(d["escalations"])), ("haiku", "opus", 2))
        self.assertEqual((d["rounds"], d["escalations"][0]["rounds"]), (0, 2))
        os.environ["AGENT_TEAM_DIR"] = self.dir
        try:
            run("journal.py", "--run", rund, "outcome", "k")
            with open(os.path.join(self.dir, "journal.jsonl")) as f:
                self.assertEqual(json.loads(f.readline())["rounds"], 2)
        finally:
            os.environ["AGENT_TEAM_DIR"] = TMP.name

    def test_dispatch_refuses_an_existing_key(self):
        args = ("state.py", "dispatch", "k2", "repo=x/y", "branch=b", "worktree=w", "issues=1", "worker=w1")
        self.assertEqual(run(*args).returncode, 0)
        run("state.py", "set", "k2", "status=IN_REVIEW", "mr=7")
        r = run(*args[:-1], "worker=w2")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("use `state.py set", r.stderr)
        with open(os.path.join(os.environ["AGENT_LOOP_DIR"], "state.json")) as f:
            d = json.load(f)["dispatches"]["k2"]
        self.assertEqual((d["status"], d["mr"], d["worker"]), ("IN_REVIEW", 7, "w1"))

    def test_set_keeps_sha_fields_as_strings(self):
        run("state.py", "dispatch", "k3", "repo=x/y", "branch=b", "worktree=w", "issues=1", "worker=w1")
        r = run("state.py", "set", "k3", "reviewed_sha=8959188", "waiting_ci=0123456", "head_sha=1,2", "rounds=2")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(os.environ["AGENT_LOOP_DIR"], "state.json")) as f:
            d = json.load(f)["dispatches"]["k3"]
        self.assertEqual((d["reviewed_sha"], d["waiting_ci"], d["head_sha"], d["rounds"]),
                         ("8959188", "0123456", "1,2", 2))
        run("state.py", "set", "k3", "reviewed_sha=null")
        with open(os.path.join(os.environ["AGENT_LOOP_DIR"], "state.json")) as f:
            self.assertIsNone(json.load(f)["dispatches"]["k3"]["reviewed_sha"])

    def test_set_nests_dotted_keys(self):
        run("state.py", "dispatch", "k4", "repo=x/y", "branch=b", "worktree=w", "issues=1", "worker=w1")
        run("state.py", "set", "k4", "park=null")
        r = run("state.py", "set", "k4", "park.kind=review", "park.note_id=44179", "park.head_sha=0123")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(os.environ["AGENT_LOOP_DIR"], "state.json")) as f:
            d = json.load(f)["dispatches"]["k4"]
        self.assertEqual(d["park"], {"kind": "review", "note_id": 44179, "head_sha": "0123"})
        self.assertNotIn("park.note_id", d)

    def test_heartbeat_waiting_ci_sets_awaiting_ci(self):
        run("state.py", "dispatch", "k5", "repo=x/y", "branch=b", "worktree=w", "issues=1", "worker=w1")
        run("state.py", "heartbeat", "k5", "HEARTBEAT", "key=k5", "sha=abc", "waiting_ci=0123456", "ready=1", "pushed")
        with open(os.path.join(os.environ["AGENT_LOOP_DIR"], "state.json")) as f:
            d = json.load(f)["dispatches"]["k5"]
        self.assertEqual((d["status"], d["waiting_ci"]), ("AWAITING_CI", "0123456"))
        run("state.py", "set", "k5", "status=MERGED")
        run("state.py", "heartbeat", "k5", "waiting_ci=fff")
        with open(os.path.join(os.environ["AGENT_LOOP_DIR"], "state.json")) as f:
            self.assertEqual(json.load(f)["dispatches"]["k5"]["status"], "MERGED")

    def test_dispatch_rejects_unknown_model(self):
        r = run("state.py", "dispatch", "k", "repo=x/y", "branch=b", "worktree=w", "issues=1", "worker=w1", "model=gpt")
        self.assertNotEqual(r.returncode, 0)

    def test_journal_add_outcome_stats(self):
        os.environ["AGENT_TEAM_DIR"] = self.dir
        try:
            run("state.py", "dispatch", "k", "repo=x/y", "branch=b", "worktree=w", "issues=1", "worker=w1", "model=haiku")
            run("state.py", "set", "k", "status=MERGED")
            self.assertEqual(run("journal.py", "outcome", "k").returncode, 0)
            for _ in range(2):
                r = run("journal.py", "add", "key=k", "class=external-api", "cause=brief", "what=missed rate limits")
                self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotEqual(run("journal.py", "add", "key=k", "class=x", "cause=vibes", "what=y").returncode, 0)
            s = run("journal.py", "stats").stdout
            self.assertIn("haiku   runs=1 merged=1 first-time-clean=1", s)
            self.assertIn("2 × external-api  <- recurring", s)
        finally:
            os.environ["AGENT_TEAM_DIR"] = TMP.name


REPO = {"id": 1, "name": "app", "mode": "merge", "hold": [], "default_branch": "main"}


def fake_api(sha, description="Closes #3"):
    def api(path, method="GET", fields=None):
        if method == "PUT":
            raise AssertionError("merge PUT must not be sent")
        if path.endswith("/merge_ref"):
            return {}
        if "pipelines" in path:
            return [{"status": "success"}]
        return {"sha": sha, "state": "opened", "draft": False, "source_branch": "b", "target_branch": "main",
                "web_url": "u", "title": "t", "description": description}
    return api


class MrState(unittest.TestCase):
    def test_numeric_reviewed_sha_does_not_crash(self):
        st = {"repos": {"g/app": REPO}}
        with mock.patch.object(mrstate, "api", fake_api("8959188abcdef")), \
                mock.patch.object(mrstate, "paged", lambda p: []):
            f = mrstate.facts(st, "g/app", 3, {"reviewed_sha": 8959188})
        self.assertFalse(f["sha_moved_since_review"])

    def test_only_operator_notes_after_the_park_count(self):
        st = {"repos": {"g/app": REPO}}
        notes = [{"id": i, "system": sys_, "author": {"username": who}, "body": "x", "created_at": "t"}
                 for i, who, sys_ in ((5, "op", False), (11, "op", False), (12, "bot", False),
                                      (13, "stranger", False), (14, "op", True))]
        paged = lambda p: notes if p.split("?")[0].endswith("/notes") else []
        with mock.patch.object(mrstate, "api", fake_api("abc")), mock.patch.object(mrstate, "paged", paged):
            f = mrstate.facts(st, "g/app", 3, {"park": {"note_id": 10}})
        self.assertEqual([n["id"] for n in f["human_notes_since_park"]], [11])

    def gate(self, description, disp, plan=None, passed=()):
        st = {"repos": {"g/app": REPO}, "team": {"passed": list(passed)}}
        with mock.patch.object(mrstate, "api", fake_api("abc", description)), \
                mock.patch.object(mrstate, "paged", lambda p: []), mock.patch.object(mrstate, "read_plan", lambda: plan):
            return mrstate.facts(st, "g/app", 3, disp)["gate_failures"]

    def test_closing_keyword_must_name_the_dispatch_issue(self):
        d = {"issues": [238]}
        self.assertEqual(self.gate("Migrates pod-a.\n\nCloses #238, re #234", d), [])
        self.assertEqual(self.gate("Fixes: #238 and #239", {"issues": [238, 239]}), [])
        self.assertEqual(self.gate("resolves g/app#238", d), [])
        self.assertEqual(self.gate("Closes #237", d), ["closes=#237 want=#238"])
        self.assertEqual(self.gate("re #238", d), ["closes=none want=#238"])
        self.assertEqual(self.gate("Closes #238\nCloses #237", d), ["closes=#237,#238 want=#238"])
        self.assertEqual(self.gate("Closes other/repo#9 and #238", d), [])
        self.assertEqual(self.gate("anything", None), [])          # untracked MR: no dispatch to compare
        url = "https://gitlab.example/g/app/-/issues/"
        self.assertEqual(self.gate(f"Closes {url}238", d), [])
        self.assertEqual(self.gate(f"Closes #238 and {url}7", d), ["closes=#7,#238 want=#238"])
        self.assertEqual(self.gate("Closes #238, app#7", d), ["closes=#7,#238 want=#238"])
        self.assertEqual(self.gate("Closes #238, https://gitlab.example/other/x/-/issues/7", d), [])

    def test_unpassed_checkpoint_and_stack_hold_the_gate(self):
        p = mkplan([task("a", checkpoint=True), task("b", deps=["a"])])
        d = {"issues": [3], "task": "b"}
        self.assertEqual(self.gate("Closes #3", d, p), ["checkpoint=a"])
        self.assertEqual(self.gate("Closes #3", d, p, passed=["a"]), [])
        self.assertEqual(self.gate("Closes #3", dict(d, base_branch="agent/1-a"), p, ["a"]), ["stacked=agent/1-a"])

    def test_branch_deleted_after_merge(self):
        def api(path, method="GET", fields=None):
            if "/repository/branches/" in path:
                raise SystemExit("glab api GET ... failed: 404 Branch Not Found")
            if "merge_requests?" in path:
                return [{"iid": 9, "state": "merged", "sha": "abc"}]
            return [{"status": "success", "web_url": "p"}]
        with mock.patch.object(mrstate, "api", api):
            b = mrstate.branch({"repos": {"g/app": REPO}}, "g/app", "agent/1-x")
        self.assertEqual((b["deleted"], b["mr"], b["mr_state"], b["sha"], b["pipeline"]), (True, 9, "merged", "abc", "success"))
        def flaky(path, method="GET", fields=None):
            if "/repository/branches/" in path:
                raise SystemExit(f"glab api GET {path} failed: glab: 502 Bad Gateway")
            return []
        with mock.patch.object(mrstate, "api", flaky), self.assertRaises(SystemExit):   # "404" in the branch name
            mrstate.branch({"repos": {"g/app": REPO}}, "g/app", "agent/404-fix")
        with mock.patch.object(mrstate, "api", lambda *a, **k: (_ for _ in ()).throw(SystemExit("glab api GET x failed: 404 Not Found")) if "branches" in a[0] else []):
            b = mrstate.branch({"repos": {"g/app": REPO}}, "g/app", "agent/1-x")
        self.assertEqual((b["deleted"], b["mr"], b["sha"], b["pipeline"]), (True, None, None, "none"))


def git(cwd, *a):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True, text=True, env=env).stdout.strip()


class LocalConflicts(unittest.TestCase):
    """GitLab's merge_ref can say ok after a sibling merge; the merge asks git on a fresh fetch."""

    def setUp(self):
        self.d = tempfile.mkdtemp(dir=TMP.name)
        self.origin, self.work, self.clone = (os.path.join(self.d, n) for n in ("origin.git", "work", "app"))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        subprocess.run(["git", "clone", "-q", self.origin, self.work], check=True, capture_output=True)
        git(self.work, "checkout", "-q", "-b", "main")
        self.commit("f.txt", "base\n", "base")
        git(self.work, "push", "-q", "origin", "main")
        subprocess.run(["git", "clone", "-q", self.origin, self.clone], check=True, capture_output=True)
        git(self.work, "checkout", "-q", "-b", "b")
        self.head = self.commit("f.txt", "branch\n", "branch")
        git(self.work, "push", "-q", "origin", "b")
        git(self.work, "checkout", "-q", "main")

    def commit(self, name, text, msg):
        with open(os.path.join(self.work, name), "w") as f:
            f.write(text)
        git(self.work, "add", name)
        git(self.work, "commit", "-q", "-m", msg)
        return git(self.work, "rev-parse", "HEAD")

    def test_clean(self):
        self.commit("other.txt", "x\n", "sibling")
        git(self.work, "push", "-q", "origin", "main")
        self.assertEqual(mrstate.local_conflicts(self.clone, "main", self.head), [])

    def test_sibling_merge_conflict_is_seen_after_fetch(self):
        self.commit("f.txt", "sibling\n", "sibling")
        git(self.work, "push", "-q", "origin", "main")
        self.assertEqual(mrstate.local_conflicts(self.clone, "main", self.head), ["f.txt"])

    def test_merge_refuses_with_rebase_verdict(self):
        self.commit("f.txt", "sibling\n", "sibling")
        git(self.work, "push", "-q", "origin", "main")
        st = {"repos": {"g/app": REPO}}
        with mock.patch.object(mrstate, "api", fake_api(self.head)), \
                mock.patch.object(mrstate, "paged", lambda p: []), \
                mock.patch.object(mrstate, "WORKSPACE", self.d):
            with self.assertRaises(SystemExit) as e:
                mrstate.merge(st, "g/app", 3, self.head)
        self.assertTrue(str(e.exception.code).startswith("REBASE app!3"), e.exception.code)

    def test_unlisted_conflict_fails_closed(self):
        def fake(cmd, **kw):
            rc, out = (1, "0123abcd\n") if "merge-tree" in cmd else (0, "")
            return subprocess.CompletedProcess(cmd, rc, out, "")
        with mock.patch.object(mrstate.subprocess, "run", fake):
            self.assertEqual(mrstate.local_conflicts(self.clone, "main", self.head), ["<unlisted conflict>"])

    def test_unfetchable_sha_reports_the_fetch(self):
        local = self.commit("g.txt", "x\n", "never pushed")
        with self.assertRaises(SystemExit) as e:
            mrstate.local_conflicts(self.clone, "main", local, 3)
        self.assertIn("refs/merge-requests/3/head", str(e.exception.code))

    def test_missing_clone_refuses(self):
        with self.assertRaises(SystemExit) as e:
            mrstate.local_conflicts(os.path.join(self.d, "nope"), "main", self.head)
        self.assertIn("no local clone", str(e.exception.code))


class SelfAccount(unittest.TestCase):
    def tearDown(self):
        common._self.clear()

    def test_env_wins_else_glab_user_once(self):
        common._self.clear()
        self.assertEqual(common.self_account(), "bot")
        common._self.clear()
        calls = []
        with mock.patch.dict(os.environ, {"AGENT_LOOP_ACCOUNT": ""}), \
                mock.patch.object(common, "api", lambda p: calls.append(p) or {"username": "me"}):
            self.assertEqual((common.self_account(), common.self_account()), ("me", "me"))
        self.assertEqual(calls, ["user"])

    def test_operator_required_and_distinct(self):
        self.assertEqual(common.operator(), "op")
        for bad in ("", "bot"):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {"AGENT_LOOP_OPERATOR": bad}):
                self.assertRaises(SystemExit, common.operator)


if __name__ == "__main__":
    unittest.main()
