# Worker brief

The issue body *is* the worker's brief. A cheap model executes precisely and does not fill gaps,
so every decision is made here; a gap comes back as `BLOCKED reason=brief-gap`, which costs a
round trip. A vague brief costs more than a long one.

## Template (every section; `plan.py check` requires all of them for haiku and sonnet)

```markdown
## Goal
One line.

## Where
Repo paths and functions, exact. An example of the same thing already done here: `path:line`.

## What to do
1. Steps, with the decisions already made: names, signatures, data formats, error codes.
   (haiku: at most 4 steps. Anything with logic: step 1 writes the tests and shows them failing.)

## Edge cases
Empty, null, 0, duplicate, repeated request, no permission, offline, …: the ones that apply.

## Do not touch
Files and areas outside this task, plus: no deploys, no production writes.

## Check
The exact command (run in a sibling container of the repo's toolchain version, `.git` mounted read-only), with no pipe and ending `; echo EXIT=$?`, and its expected result.

## Done when
A verifiable criterion.

## Known pitfalls
1–5 items for this area: the worker starts from zero and knows none of them.
```

## Task classes → mandatory brief content

If a task matches a row, the brief must cover it. Rows come from the worker journal
(`journal.py stats`): a failure class seen twice gets a row here, or a check if a command can catch it.

| Task class | The brief must say |
|---|---|
| Protocol or CLI adapter | What happens on unknown messages, and cancel at every phase. |
| External API | Its rate limits, error codes and retry policy. |
| Two sides of an interface built in parallel | A contract or fixture task first; both sides depend on it. |
| Real-time or long-running status | Behaviour when the connection drops mid-process. |
| Money or measurements with units | Integer minor units (or exact decimals), never float; a fractional test case. |
| Shared UI component | Every screen that uses it, and a screenshot of each after the change. |
| Identity, auth, permissions | The exact identity source; never "copy the pattern in X". Opus only. |
| Schema or file format | Forward and backward compatibility; unknown fields and types are kept or shown, never silently dropped. |
| A persisted setting or toggle with async writes | Concurrent writes are serialised and the last toggle wins; a test for a rapid off→on (and on→off) that checks the stored value, not only the screen. |
| Offline-capable or PWA | What happens with no network, and on the update after reconnecting. |
| CI job or script that runs in an image | Every binary the script calls exists in that image: check with `docker run --rm --entrypoint sh <image> -c 'command -v git crane …'` before writing the brief. |
| A rule the code already defines (hash conventions, key grammar, validation) | Point at the code that defines it (`path:line`, the predicate to import); never restate the rule in prose, because a restatement can be wrong. |
| Docs with numbered references (decision rows, sections, steps) | The exact numbers to add and every number to cite; never "the new row" or "the next number". |
| A decision about what an external service accepts (auth, endpoints, limits) | Where it was verified (`file:line` in its source, or a doc URL); unverified → verify before briefing. |
| Adds or changes something a doc enumerates (routes, deployables, env vars, jobs) | Every table or list that enumerates it (`README.md` deployables, `CLAUDE.md` sections), named by path. |
| UI screen or restyle | Browser checks run on the **final head**, after any rebase; render every variant the code branches on (e.g. read-only, multi-part, empty), not only the fixture's default. |
| UI restructure that removes chrome (header, nav, back link) | Every route and action the removed chrome exposed, and where each lives afterwards. |
| Behaviour only a real device can show (stylus, touch, OS chrome, PWA install) | The device check the operator will run, step by step, and the on-screen diagnostics that check reads, with every competing explanation it must tell apart. The diagnostics ship in the same MR, refreshed on every relevant event, and the build shows its version. |
| Navigation or routing change | The router and the component key the route mounts under (`path:line`); if the change needs them, they are in the file list, never in "Do not touch". |
| A test or module that imports another workspace (e.g. app tests importing `server/src`) | Every build context that typechecks the importing workspace (Dockerfiles, `tsconfig.build.json` excludes) and any config line the repo `CLAUDE.md` requires for it, in the file list. |
| Changes a function several paths share (merge, import, sync) | Every caller path, and the rule per path, checked against the plan decisions those paths carry. |
| Mockups, screenshots or other binaries | None in the MR diff (review bots cap diff size and read bytes as text); cite them from the issue or a branch. Worker screenshots stay on disk under the worktree for the monitor to review there; never attached or uploaded to the MR (`glab` can't multipart-upload, and workers must not read the token). |
| Every task with a Check | The command without a pipe (`\| tail` hides the exit code), ending `; echo EXIT=$?`; the worker reports that exit code. |
| Tests first (red-first) | The worker pastes the failing run's output (assertions and `EXIT=`) into the MR description. |
| A test that asserts the code already enforces a rule (validator, schema check, guard) | The source line that enforces it (`path:line`), checked by the brief writer; if no line enforces it, the task adds the rule. |
| Anything with a `.claude/verify.sh` | Its dependencies must be installed in the worktree first: the stop hook runs it offline. |
| Port of a policy or rule set to a new engine | The legacy policy's verdicts on a truth-table fixture (field absent / empty / mixed values, a look-alike name, an indirectly created object), run with the pinned CLI and pasted into the brief. Exact names port as `==`, never a prefix match. |
| CI check that parses expressions (CEL, regex, templates) for parity | Reject any residue left after stripping the recognised literals; a negative control that goes red, shown in the MR. |
| Policy test suite | A structural CI invariant on each policy's match scope: a policy CLI may report a policy that matches nothing as passing, so fail rows can't catch a mis-scoped match. |
| A step that relies on a waiver of a standing rule | The operator's waiver, given to the orchestrator directly before the plan relies on it; a worker's permission system refuses a waiver an agent relays. |
| A mid-run brief edit that assumes a tool can do something | Checked against that tool before the edit goes out (as skill step 1.4), not only at planning. |
