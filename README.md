# Claude Code agent skills

Three Claude Code skills, the two agent definitions they use, and a stop hook:

| Path | What it is |
|---|---|
| `skills/agent-team/` | `/agent-team <group/repo>#<iid>`: plans a large piece of work, waits for your approval of the plan (tasks, a model per task, worker count), opens one issue per task, then runs parallel workers until every MR is merged or parked. |
| `skills/agent-loop/` | `/agent-loop <group/repo>:<merge\|review> …`: the loop agent-team runs on. It picks up issues with a queue label, one worker per issue, and a monitor reviews and (if you allow it) merges. `PROTOCOL.md` is the contract between the roles; `bin/` holds the scripts. |
| `agents/monitor.md`, `agents/worker.md` | The subagents the loop spawns. |
| `hooks/agent-verify/` | A `SubagentStop` hook: a worker can't end its turn while its worktree's `.claude/verify.sh` fails. |
| `skills/resume-session/` | `/resume-session <issue, name, topic, ID or link>`: brings a suspended `claude --bg` job or a Remote Control chat back to life, with its history. Independent of the others. |

## Requirements

- Claude Code with subagents, `SendMessage`, `ScheduleWakeup` and (for alerts) `PushNotification`.
- **GitLab only.** Everything goes through [`glab`](https://gitlab.com/gitlab-org/cli), logged in as the account the agents post as. There is no GitHub support.
- Python 3.11 or newer (stdlib only), git 2.38 or newer (merges check conflicts with `git merge-tree --write-tree`), and Docker for the stop hook and for running repo tests in containers.
- **A separate GitLab account for the agents.** They post, push and open MRs as that account; your own account reviews. The scripts refuse to start when the two are the same.
- `resume-session` needs only Python and the `claude` CLI.

## Install

Copy the folders into your Claude Code config directory:

```sh
cp -r skills/agent-team skills/agent-loop skills/resume-session ~/.claude/skills/
cp agents/monitor.md agents/worker.md ~/.claude/agents/
install -m 0755 hooks/agent-verify/agent-verify /usr/local/bin/agent-verify   # or anywhere on PATH
```

Then set:

| Variable | Needed | Meaning |
|---|---|---|
| `AGENT_LOOP_OPERATOR` | yes | Your GitLab username. Workers request your review on every MR, park notes mention you, and only your notes and comments count as instructions; everyone else's are data. |
| `AGENT_LOOP_ACCOUNT` | no | The GitLab account the agents post as. Defaults to `glab`'s logged-in user. |
| `AGENT_VERIFY_TIMEOUT` | no | Seconds the stop hook lets `verify.sh` run. Default 240; the hook's own budget is 480. |
| `CLAUDE_CONFIG_DIR` | no | Read by `usage.py` to find `.credentials.json`, as Claude Code does. Default `~/.claude`. |

Everything lives under `~/workspace`: repo clones, one worktree per branch (`<repo>--<branch>`), `_agent-loop/` (loop state) and `_agent-team/` (team runs, the worker journal, the stop hook's cache). The skill docs assume that path, so create it and start the session there.

The monitor runs on `fable` and workers default to `opus` (team runs pick `haiku`, `sonnet` or `opus` per task). Change `model:` in `agents/*.md` to suit your plan.

`/agent-team` step 1 calls `/grill-me` when the operator still has decisions to make. That skill isn't included here: install one or drop the line.

## agent-verify

Register the hook in `~/.claude/settings.json` (or a managed settings file):

```json
{"hooks": {"SubagentStop": [{"hooks": [{"type": "command", "command": "/usr/local/bin/agent-verify", "timeout": 480}]}]}}
```

It acts only on a subagent of type `worker` whose transcript starts with a `DISPATCH`/`REATTACH` briefing, and only on a worktree under `AGENT_VERIFY_WORKSPACE` named `<repo>--<branch>`. If that worktree has `.claude/verify.sh`, the hook runs it in a sibling container and, while it fails, sends the worker back with the output once (a second failure lets it stop and say so). The script names its image on one of its first lines:

```sh
# verify-image: docker.io/library/node:22
npm test
```

- `verify.sh` is untrusted repo content. It runs only in a `--network none` container with the worktree mounted and its `.git` read-only, never in the hook's own shell.
- **A writable `.git` is code execution on the host** (a repo can set `core.fsmonitor`, and the next `git status` runs it). The read-only `.git` mount, the `gitdir:` shape check and the hook's own git hardening each stop it; each has a test. Keep all three.
- It never wedges a worker: any unexpected input exits 0.

## usage.py

`skills/agent-loop/bin/usage.py` reads how much of your 5-hour and weekly usage is left, so a team run can hold or stop before the limit. It reads the Claude Code OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`, else `~/.claude/.credentials.json`) and sends it only to `https://api.anthropic.com/api/oauth/usage`, an endpoint Claude Code's `/usage` uses that isn't publicly documented. Only the main session runs it; never hand it to a subagent. Without an OAuth login it prints `USAGE UNKNOWN` and the run asks you instead.

## Safety model

- The monitor merges only on repos you mark `merge`, only through `mrstate.py --merge`, and only when the gate passes (CI green on the head SHA, no open threads, no hold-glob hit). `review` repos park every MR for you.
- Subagents can't ask you anything. `PROTOCOL.md` "Hard rules" is the stop list (force-pushes to shared branches, history rewrites, discarding others' work, exec-capable git config, destructive GitLab calls): an agent that needs one reports `BLOCKED` and the item parks for you.
- MR notes, issue text from anyone but `AGENT_LOOP_OPERATOR`, and diffs are treated as data, never as instructions.

## Tests

```sh
python3 -W error::ResourceWarning -m unittest discover -s skills/agent-loop/bin
python3 -W error::ResourceWarning -m unittest discover -s hooks/agent-verify
```

Both suites use a fake `docker` and real `git` (needs `user.name` / `user.email` set).
