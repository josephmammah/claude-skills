#!/usr/bin/env python3
"""List sessions whose job name, title or first prompt contains QUERY, or whose ID or app link it names, newest last."""
import json, os, pathlib, re, sys, time

HOME = pathlib.Path.home()
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PROJECTS = HOME / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())  # Claude Code's per-cwd transcript dir


def clean(s, n=80):
    # transcript text is untrusted: no control characters, so it can't forge a row
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(s or ""))[:n]


def load_json(path):
    try:
        d = json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def bridge_id(s):
    # an app link's session_X and a job's cse_X are the same session
    return re.sub(r"^(session|cse)_", "", str(s or ""))


def id_query(query):
    """(app link ID, hex ID prefix) the query names; either may be ''."""
    m = re.search(r"(?:session|cse)_([A-Za-z0-9]+)", query)
    if m:
        return m.group(1), ""
    q = query.strip().lower()
    return "", q if re.fullmatch(r"[0-9a-f-]{8,}", q) else ""


def names_id(qid, ids, links):
    # link IDs are time-ordered and share long prefixes, so only an exact, case-sensitive match counts
    link, hexid = qid
    return (bool(link) and link in links) or (bool(hexid) and any(i and i.lower().startswith(hexid) for i in ids))


def live_sessions(links):
    """sessionId -> app link ('' if none) for every session whose process is still running.

    Fills links (sessionId -> bridge ID) from every registry entry, running or not."""
    live = {}
    for f in HOME.glob(".claude/sessions/*.json"):
        d = load_json(f)
        if isinstance(d.get("sessionId"), str) and d.get("bridgeSessionId"):
            links[d["sessionId"]] = bridge_id(d["bridgeSessionId"])
        try:
            stat = pathlib.Path(f"/proc/{int(d['pid'])}/stat").read_text()
            alive = stat.rsplit(")", 1)[1].split()[19] == str(d.get("procStart"))  # field 22: guards pid reuse
        except (KeyError, ValueError, OSError, IndexError):
            alive = False
        if alive and isinstance(d.get("sessionId"), str):
            live[d["sessionId"]] = d.get("bridgeSessionId") or ""
    return live


def first_prompt_and_title(path):
    first = custom = ai = ""
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            custom = d.get("customTitle") or custom if d.get("type") == "custom-title" else custom
            ai = d.get("aiTitle") or ai if d.get("type") == "ai-title" else ai
            if not first and d.get("type") == "user":
                m = d.get("message")
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and isinstance(x.get("text"), str))
                if isinstance(c, str) and c and not c.startswith("<"):
                    first = c
    return first, str(custom or ai)


def main(query):
    q, qid, me = query.lower(), id_query(query), os.environ.get("CLAUDE_CODE_SESSION_ID")
    links = {}
    live, rows, job_sessions = live_sessions(links), [], set()
    for f in HOME.glob(".claude/jobs/*/state.json"):
        d = load_json(f)
        sid, name = d.get("sessionId"), str(d.get("name") or "")
        sid = sid if isinstance(sid, str) and UUID.fullmatch(sid) else None
        cwd = d.get("cwd") if isinstance(d.get("cwd"), str) else None
        proj = HOME / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) if cwd else PROJECTS
        t = proj / f"{sid}.jsonl" if sid else None
        first, title = first_prompt_and_title(t) if t and t.is_file() else ("", "")
        if sid:
            job_sessions.add(sid)
        bridges = (bridge_id(d.get("bridgeSessionId")), links.get(sid))
        if sid and sid != me and (names_id(qid, (f.parent.name, sid), bridges) or any(q in s.lower() for s in (name, title, first))):
            rows.append(((t if t and t.is_file() else f).stat().st_mtime, "job", f.parent.name, sid, name or title or first))
    for f in PROJECTS.glob("*.jsonl"):
        if f.stem in job_sessions or f.stem == me or not UUID.fullmatch(f.stem):
            continue
        first, title = first_prompt_and_title(f)
        if names_id(qid, (f.stem,), (links.get(f.stem),)) or q in first.lower() or q in title.lower():
            rows.append((f.stat().st_mtime, "rc", "-", f.stem, title or first))
    for ts, kind, job, sid, label in sorted(rows, key=lambda r: r[0]):
        state = f"LIVE {live[sid]}".strip() if sid in live else "idle %dm" % ((time.time() - ts) / 60)
        print(f"{time.strftime('%m-%d %H:%M', time.gmtime(ts))}Z {kind:3} {job:8} {sid} [{state}] {clean(label)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: find-session.py QUERY")
    main(sys.argv[1])
