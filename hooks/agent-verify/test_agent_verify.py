"""agent-verify with a fake `docker` on PATH and real git worktrees in a temp workspace."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-verify")
IMAGE = "docker.io/library/node:22"
FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1" in rm|image) exit 0;; pull) exit 0;; esac
[ -f "$FAKE_SLEEP" ] && sleep 5
cat "$FAKE_OUT" 2>/dev/null
exit "$(cat "$FAKE_RC" 2>/dev/null || echo 0)"
"""


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.ws = os.path.join(t, "ws")
        self.bin = os.path.join(t, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "docker"), "w") as f:
            f.write(FAKE_DOCKER)
        os.chmod(os.path.join(self.bin, "docker"), 0o755)
        self.wt = os.path.join(self.ws, "app--agent-1-x")
        os.makedirs(os.path.join(self.wt, ".claude"))
        self.git("init", "-q")
        self.verify(f"#!/bin/sh\n# verify-image: {IMAGE}\nnpm test\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "x")
        self.log = os.path.join(t, "docker.log")
        self.rc = os.path.join(t, "rc")
        self.env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], AGENT_VERIFY_WORKSPACE=self.ws,
                        AGENT_VERIFY_CACHE=os.path.join(t, "cache"), FAKE_LOG=self.log, FAKE_RC=self.rc,
                        FAKE_OUT=os.path.join(t, "out"), FAKE_SLEEP=os.path.join(t, "sleep"))

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *a):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "-C", self.wt, *a], check=True, capture_output=True, env=env)

    def verify(self, body):
        with open(os.path.join(self.wt, ".claude", "verify.sh"), "w") as f:
            f.write(body)

    def transcript(self, first):
        p = os.path.join(self.tmp.name, "agent.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": first}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": f"DISPATCH key=z worktree={self.ws}/other"}}) + "\n")
        return p

    def run_hook(self, worktree=None, agent_type="worker", active=False, first=None, event="SubagentStop"):
        first = first or f"DISPATCH key=app#1 repo=g/app branch=agent/1-x worktree={worktree or self.wt} issues=1"
        payload = {"hook_event_name": event, "agent_type": agent_type, "agent_id": "a1",
                   "stop_hook_active": active, "agent_transcript_path": self.transcript(first)}
        return subprocess.run([sys.executable, "-I", HOOK], input=json.dumps(payload), capture_output=True,
                              text=True, env=self.env, timeout=60)

    def calls(self, all_calls=False):
        try:
            with open(self.log) as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        return lines if all_calls else [c for c in lines if c.startswith("run ")]

    def red(self):
        with open(self.rc, "w") as f:
            f.write("1")

    def test_green_passes_and_runs_sandboxed(self):
        p = self.run_hook()
        self.assertEqual(p.returncode, 0, p.stderr)
        (call,) = self.calls()
        self.assertIn("--network none", call)
        self.assertIn(f"type=bind,src={os.path.realpath(self.wt)},dst=/work", call)
        self.assertIn(f"type=bind,src={os.path.realpath(self.wt)}/.git,dst=/work/.git,readonly", call)
        self.assertIn(f"--entrypoint sh {IMAGE} .claude/verify.sh", call)

    def test_green_result_is_cached_until_the_tree_changes(self):
        self.run_hook()
        self.run_hook()
        self.assertEqual(len(self.calls()), 1)
        with open(os.path.join(self.wt, "new.ts"), "w") as f:
            f.write("x")
        self.run_hook()
        self.assertEqual(len(self.calls()), 2)

    def test_red_blocks_then_lets_go(self):
        self.red()
        p = self.run_hook()
        self.assertEqual(p.returncode, 2)
        self.assertIn("failed (exit 1)", p.stderr)
        p = self.run_hook(active=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("still red", p.stderr)

    def test_red_is_never_cached(self):
        self.red()
        self.run_hook()
        self.run_hook()
        self.assertEqual(len(self.calls()), 2)

    def test_timeout_kills_container_and_blocks(self):
        open(self.env["FAKE_SLEEP"], "w").close()
        self.env["AGENT_VERIFY_TIMEOUT"] = "1"
        p = self.run_hook()
        self.assertEqual(p.returncode, 2)
        self.assertIn("did not finish", p.stderr)
        self.assertTrue(any(c.startswith("rm -f agent-verify-") for c in self.calls(all_calls=True)))

    def test_ignored_cases(self):
        for kw in ({"agent_type": "monitor"}, {"agent_type": "general-purpose"}, {"event": "Stop"},
                   {"first": "Review MR !3 please. DISPATCH key=x worktree=" + self.wt},
                   {"worktree": "/etc"}, {"worktree": self.ws + "/../escape"}, {"worktree": self.ws + "/missing"},
                   {"worktree": self.ws}):
            with self.subTest(**kw):
                self.assertEqual(self.run_hook(**kw).returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_symlinked_worktree_outside_workspace_is_ignored(self):
        outside = tempfile.mkdtemp(dir=self.tmp.name)
        os.symlink(outside, os.path.join(self.ws, "link"))
        self.assertEqual(self.run_hook(worktree=os.path.join(self.ws, "link")).returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_no_or_bad_image_line_skips(self):
        for body in ("#!/bin/sh\nnpm test\n", "#!/bin/sh\n# verify-image: node:22\n",
                     "#!/bin/sh\n# verify-image: docker.io/x;rm -rf /\n", "\n\n\n\n\n# verify-image: docker.io/a/b:1\n"):
            with self.subTest(body=body):
                self.verify(body)
                self.red()
                self.assertEqual(self.run_hook().returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_symlinked_verify_script_is_ignored(self):
        os.remove(os.path.join(self.wt, ".claude", "verify.sh"))
        os.symlink("/etc/hostname", os.path.join(self.wt, ".claude", "verify.sh"))
        self.assertEqual(self.run_hook().returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_untracked_content_change_invalidates_cache(self):
        with open(os.path.join(self.wt, "new.ts"), "w") as f:
            f.write("a")
        self.run_hook()
        with open(os.path.join(self.wt, "new.ts"), "w") as f:
            f.write("b")
        self.run_hook()
        self.assertEqual(len(self.calls()), 2)

    def test_non_worktree_names_are_ignored(self):
        for name in ("_scratch", "plain", "a--b,readonly=false"):
            with self.subTest(name=name):
                d = os.path.join(self.ws, name)
                os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
                os.makedirs(os.path.join(d, ".git"), exist_ok=True)
                with open(os.path.join(d, ".claude", "verify.sh"), "w") as f:
                    f.write(f"# verify-image: {IMAGE}\n")
                self.assertEqual(self.run_hook(worktree=d).returncode, 0)
        self.assertEqual(self.calls(), [])

    def evil_gitdir(self):
        """A gitdir whose config makes any git status run a program that drops a marker."""
        evil = os.path.join(self.ws, "_evil")
        subprocess.run(["git", "init", "-q", "--bare", evil], check=True, env=self.genv())
        marker = os.path.join(self.tmp.name, "PWNED")
        with open(os.path.join(evil, "config"), "a") as f:
            f.write(f"[core]\n\tfsmonitor = touch {marker}; false\n\tbare = false\n")
        return evil, marker

    def genv(self):
        return dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def test_repointed_dotgit_file_is_refused(self):
        evil, marker = self.evil_gitdir()
        main = os.path.join(self.ws, "app")
        subprocess.run(["git", "clone", "-q", self.wt, main], check=True, env=self.genv())
        linked = os.path.join(self.ws, "app--agent-2-y")
        subprocess.run(["git", "-C", main, "worktree", "add", "-q", "-b", "y", linked], check=True, env=self.genv())
        self.assertEqual(self.run_hook(worktree=linked).returncode, 0)   # a real linked worktree is accepted
        self.assertEqual(len(self.calls()), 1)
        with open(os.path.join(linked, ".git"), "w") as f:            # what a hostile verify.sh would write
            f.write(f"gitdir: {evil}\n")
        os.makedirs(os.path.join(linked, "x"), exist_ok=True)
        self.assertEqual(self.run_hook(worktree=linked).returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(os.path.exists(marker))

    def test_hook_git_ignores_fsmonitor_in_repo_config(self):
        _, marker = self.evil_gitdir()
        with open(os.path.join(self.wt, ".git", "config"), "a") as f:
            f.write(f"[core]\n\tfsmonitor = touch {marker}; false\n")
        with open(os.path.join(self.wt, "new.ts"), "w") as f:
            f.write("x")
        self.assertEqual(self.run_hook().returncode, 0)
        self.assertFalse(os.path.exists(marker))

    def test_garbage_input_never_blocks(self):
        p = subprocess.run([sys.executable, "-I", HOOK], input="not json", capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
