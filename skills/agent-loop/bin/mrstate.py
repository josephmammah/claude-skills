#!/usr/bin/env python3
"""Per-dispatch MR facts and decisions, all from the API, nothing inferred.

Usage: mrstate.py [--json]                        every live dispatch with an MR
       mrstate.py --mr <repo-path> <iid> [--json]   one MR, whether tracked or not
       mrstate.py --branch <repo-path> <branch>     head sha + pipeline status for a branch (pre-MR CI wait)
       mrstate.py --merge <repo-path> <iid> <sha>   re-check the gate, merge, verify by fresh GET
       mrstate.py --foreign [--json]               open MRs on tracked repos that no dispatch owns

Gate (merge mode only): pipeline success for the exact head sha, zero open resolvable threads,
merge_ref 200, not draft, targets the default branch, no hold glob hit; for a tracked dispatch also
the description's closing keywords name exactly its issues, no unpassed checkpoint on its task's
dependencies, and no `base_branch` left (a stacked MR rebases first). `--merge` then fetches the
local clone (~/workspace/<repo-name>, $AGENT_LOOP_WORKSPACE) and refuses with `REBASE <key>: …`
when `git merge-tree` finds a conflict with origin/<target>: merge_ref can be stale after a sibling merge.
"""
import re
import sys
from common import *

# GitLab's default issue closing pattern: #n, [group/]project#n and issue URLs.
CLOSING = re.compile(r"\b(?:clos(?:e[sd]?|ing)|fix(?:e[sd]|ing)?|resolv(?:e[sd]?|ing)|implement(?:s|ed|ing)?):? +"
                     r"((?:(?:issues? +)?(?:https?://[^\s,]+/issues/\d+|[\w./-]*#\d+)(?: *,? +and +| *,? *)?)+)", re.I)
REF = re.compile(r"https?://[^/\s]+/([^\s,]+?)(?:/-)?/issues/(\d+)|([\w./-]*)#(\d+)")


def closes(description, repo):
    """Issue iids in `repo` that the MR description closes on merge (other projects' refs are skipped)."""
    own = ("", repo, repo.rsplit("/", 1)[-1])
    return sorted({int(r.group(2) or r.group(4)) for m in CLOSING.finditer(description or "")
                   for r in REF.finditer(m.group(1)) if (r.group(1) if r.group(2) else r.group(3)) in own})


def disp_for(st, path, iid):
    return next((d for d in st.get("dispatches", {}).values() if d["repo"] == path and d.get("mr") == iid), None)


def facts(st, path, iid, disp=None):
    r = st["repos"][path]
    pid = r["id"]
    mr = api(f"projects/{pid}/merge_requests/{iid}")
    sha = mr["sha"]
    pipes = api(f"projects/{pid}/pipelines?sha={sha}&per_page=1")
    disc = paged(f"projects/{pid}/merge_requests/{iid}/discussions")
    open_threads = [d["id"] for d in disc
                    if d["notes"][0].get("resolvable") and not d["notes"][0].get("resolved")]
    try:
        api(f"projects/{pid}/merge_requests/{iid}/merge_ref")
        merge_ref = "ok"
    except SystemExit as e:
        merge_ref = "conflict" if "400" in str(e) or "409" in str(e) else f"error: {e}"
    changed = [c["new_path"] for c in paged(f"projects/{pid}/merge_requests/{iid}/diffs")]
    hold = hold_hit(changed, r["hold"])
    notes = paged(f"projects/{pid}/merge_requests/{iid}/notes?sort=asc&order_by=created_at")
    park_after = (disp or {}).get("park", {}).get("note_id", 0)
    human_notes = [{"id": n["id"], "author": n["author"]["username"], "body": n["body"][:500],
                    "created_at": n["created_at"]}
                   for n in notes if not n["system"] and n["author"]["username"] == operator() and n["id"] > park_after]
    f = {"repo": path, "iid": iid, "key": f"{r['name']}!{iid}", "state": mr["state"], "draft": mr["draft"],
         "source_branch": mr["source_branch"], "target_branch": mr["target_branch"], "sha": sha,
         "pipeline": pipes[0]["status"] if pipes else "none", "open_threads": open_threads,
         "merge_ref": merge_ref, "hold_hit": hold, "changed_files": len(changed),
         "human_notes_since_park": human_notes, "web_url": mr["web_url"], "title": mr["title"]}
    gate = []
    if f["pipeline"] != "success": gate.append(f"pipeline={f['pipeline']}")
    if open_threads: gate.append(f"threads={len(open_threads)}")
    if merge_ref != "ok": gate.append(f"merge_ref={merge_ref}")
    if mr["draft"]: gate.append("draft")
    if mr["target_branch"] != r["default_branch"]: gate.append(f"target={mr['target_branch']}")
    if hold: gate.append(f"hold={','.join(hold)}")
    if disp is not None and disp.get("issues"):
        got, want = closes(mr.get("description"), path), sorted(disp["issues"])
        if got != want:
            gate.append(f"closes={','.join(f'#{n}' for n in got) or 'none'} want={','.join(f'#{n}' for n in want)}")
    if disp is not None and disp.get("task") and st.get("team"):
        held = checkpoint_hold(read_plan(), st["team"].get("passed", []), disp["task"])
        if held: gate.append(f"checkpoint={','.join(held)}")
    if disp is not None and disp.get("base_branch"):
        gate.append(f"stacked={disp['base_branch']}")
    f["gate_failures"] = gate
    f["gate_ok"] = not gate
    f["mode"] = r["mode"]
    if disp is not None:
        reviewed = disp.get("reviewed_sha")
        f["reviewed_sha"] = reviewed
        f["sha_moved_since_review"] = bool(reviewed) and not sha.startswith(str(reviewed))
    return f


def local_conflicts(clone, target, sha, iid=None):
    """Paths that conflict when `sha` merges into a freshly fetched origin/<target>; [] = clean."""
    if not os.path.exists(os.path.join(clone, ".git")):
        raise SystemExit(f"no local clone at {clone}: `git clone <web_url>.git` it there, then merge again")
    def git(*a):
        return subprocess.run(["git", "-C", clone, *a], capture_output=True, text=True, env=ENV)
    f = git("fetch", "--quiet", "origin")
    if f.returncode:
        raise SystemExit(f"git fetch in {clone} failed: {f.stderr.strip()[:300]}")
    if git("cat-file", "-e", f"{sha}^{{commit}}").returncode:   # merge-tree exits 1 on an unknown sha too
        ref = f"refs/merge-requests/{iid}/head"
        f = git("fetch", "--quiet", "origin", ref) if iid is not None else None
        if f is None or f.returncode or git("cat-file", "-e", f"{sha}^{{commit}}").returncode:
            why = f.stderr.strip()[:300] if f is not None else "no MR iid to fetch"
            raise SystemExit(f"{sha} is not in {clone} after fetching origin and {ref}: {why}")
    m = git("merge-tree", "--write-tree", "--name-only", "--no-messages", f"origin/{target}", sha)
    if m.returncode == 0:
        return []
    if m.returncode == 1:   # not clean: fail closed even if no path is listed
        return [p for p in m.stdout.splitlines()[1:] if p] or ["<unlisted conflict>"]
    raise SystemExit(f"git merge-tree failed (needs git >= 2.38): {m.stderr.strip()[:300]}")


def merge(st, path, iid, sha):
    r = st["repos"][path]
    if r["mode"] != "merge":
        raise SystemExit(f"{path} is review-only; never merge")
    f = facts(st, path, iid, disp_for(st, path, iid))
    if f["sha"] != sha:
        raise SystemExit(f"head moved: expected {sha}, MR has {f['sha']}")
    if not f["gate_ok"]:
        raise SystemExit("gate failed: " + " ".join(f["gate_failures"]))
    clash = local_conflicts(os.path.join(WORKSPACE, r["name"]), f["target_branch"], sha, iid)
    if clash:
        raise SystemExit(f"REBASE {f['key']}: conflicts with origin/{f['target_branch']} in {', '.join(clash)} "
                         "although merge_ref said ok; send REBASE, do not merge")
    api(f"projects/{r['id']}/merge_requests/{iid}/merge", "PUT", {"sha": sha})
    after = api(f"projects/{r['id']}/merge_requests/{iid}")
    if after["state"] != "merged":
        raise SystemExit(f"merge PUT returned but state is {after['state']}")
    print(json.dumps({"merged": True, "key": f["key"], "sha": sha, "merge_commit_sha": after.get("merge_commit_sha")}))


def branch(st, path, name):
    r = st["repos"][path]
    try:
        sha, gone = api(f"projects/{r['id']}/repository/branches/{enc(name)}")["commit"]["id"], {"deleted": False}
    except SystemExit as e:
        if "404" not in str(e).split(" failed: ", 1)[-1]:   # the path before it holds the branch name
            raise
        mrs = api(f"projects/{r['id']}/merge_requests?source_branch={enc(name)}&state=all&per_page=1")  # newest first
        mr = mrs[0] if mrs else {}
        sha, gone = mr.get("sha"), {"deleted": True, "mr": mr.get("iid"), "mr_state": mr.get("state")}
    pipes = api(f"projects/{r['id']}/pipelines?sha={sha}&per_page=1") if sha else []
    return {"repo": path, "branch": name, "sha": sha, "pipeline": pipes[0]["status"] if pipes else "none",
            "pipeline_url": pipes[0]["web_url"] if pipes else None, **gone}


def foreign(st):
    owned = {(d["repo"], d["branch"]) for d in st["dispatches"].values()}
    rows = []
    for path, r in st["repos"].items():
        for mr in api(f"projects/{r['id']}/merge_requests?state=opened&per_page=100"):
            if (path, mr["source_branch"]) not in owned:
                rows.append({"repo": path, "iid": mr["iid"], "author": mr["author"]["username"],
                             "branch": mr["source_branch"], "title": mr["title"]})
    return rows


def human(rows):
    if not rows:
        print("NO TRACKED MRS")
    for f in rows:
        gate = "GATE_OK" if f["gate_ok"] else "GATE_FAIL(" + " ".join(f["gate_failures"]) + ")"
        print(f"{f['key']:30} {f['sha'][:10]} {f['state']:7} {f['mode']:6} {gate} "
              f"human_notes={len(f['human_notes_since_park'])} | {f['title'][:50]}")


if __name__ == "__main__":
    st = load_state()
    a = [x for x in sys.argv[1:] if x != "--json"]
    if a[:1] == ["--mr"]:
        out(facts(st, a[1], int(a[2]), disp_for(st, a[1], int(a[2]))), lambda f: human([f]))
    elif a[:1] == ["--branch"]:
        out(branch(st, a[1], a[2]), lambda b: print(
            f"{b['repo']} {b['branch']} {(b['sha'] or '-')[:10]} pipeline={b['pipeline']}"
            + (f" DELETED mr={b['mr']} {b['mr_state']}" if b["deleted"] else "")))
    elif a[:1] == ["--merge"]:
        merge(st, a[1], int(a[2]), a[3])
    elif a[:1] == ["--foreign"]:
        out(foreign(st), lambda rows: [print(f"FOREIGN {r['repo']}!{r['iid']} by {r['author']} [{r['branch']}] {r['title'][:60]}") for r in rows] or (rows or print("NO FOREIGN MRS")))
    else:
        rows = [facts(st, d["repo"], d["mr"], d) for d in st["dispatches"].values()
                if d.get("mr") and d["status"] not in ("MERGED", "RETIRED") and d["repo"] in st["repos"]]
        dropped = [k for k, d in st["dispatches"].items() if d["repo"] not in st["repos"]]
        if dropped:
            print(f"WARNING: {len(dropped)} dispatch(es) on repos no longer configured: {', '.join(dropped)}", file=sys.stderr)
        out(rows, human)
