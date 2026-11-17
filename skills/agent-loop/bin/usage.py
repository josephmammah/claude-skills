#!/usr/bin/env python3
"""Plan usage left: the 5-hour window and the week, as Claude Code's /usage shows them.

Usage: usage.py [--json] [--run <dir>] [--max-5h N] [--stop-5h N] [--max-week N]

--run reads the run's plan.json `usage_stop_5h` (the operator's hard stop for that run): it
becomes --stop-5h, and the hold moves to 5 points below it. Explicit flags win.

Prints one line per limit, then a verdict line the supervisor acts on:
  USAGE OK
  USAGE STOP until=<iso> reason=<…>   5h window at or over --stop-5h (default 95): stop the monitor and live workers
  USAGE HOLD until=<iso> reason=<…>   5h window at or over --max-5h (default 90): dispatch nothing new until the reset
  USAGE ASK until=<iso> reason=<…>    a weekly limit at or over --max-week (default 90), or a 5h HOLD with no
                                      reset time: the operator decides
  USAGE UNKNOWN reason=<…>            bad arguments, or no credential, scope or answer (exit 3)

Supervisor only (agent-team SKILL.md): it reads the session's OAuth token and sends it to
api.anthropic.com and nowhere else. Stdlib only.
"""
import json, os, sys, urllib.error, urllib.request
from datetime import datetime, timezone

URL = "https://api.anthropic.com/api/oauth/usage"
MAX_5H, STOP_5H, MAX_WEEK = 90, 95, 90   # the operator's standing gates (agent-team SKILL.md §5)


def token():
    """The token claude itself uses: the env var wins over the stored login, as in Claude Code."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    cfg = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
    try:
        with open(os.path.join(cfg, ".credentials.json")) as f:
            return (json.load(f).get("claudeAiOauth") or {}).get("accessToken")
    except (OSError, ValueError, AttributeError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # urllib would resend the Authorization header to the new host


def fetch(tok):
    req = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code-agent-loop"})
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=20) as r:
        data = json.load(r)
    if not isinstance(data, dict):
        raise ValueError("answer is not an object")
    return data


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def _in(dt, now):
    if not dt:
        return "?"
    m = max(0, int((dt - now).total_seconds() // 60))
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    return f"{d}d{h}h" if d else f"{h}h{m:02d}m" if h else f"{m}m"


def limits(data):
    """[(name, percent used, reset datetime, is_weekly)] — 5h, the week, then any per-model weekly limit."""
    out = []
    for key, name, weekly in (("five_hour", "5h", False), ("seven_day", "week", True)):
        v = data.get(key)
        if v and v.get("utilization") is not None:
            out.append((name, float(v["utilization"]), _ts(v.get("resets_at")), weekly))
    for lim in data.get("limits") or []:
        if lim.get("kind") == "weekly_scoped" and lim.get("percent") is not None:
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name") or "scoped"
            out.append((f"{model} week", float(lim["percent"]), _ts(lim.get("resets_at")), True))
    return out


def verdict(lims, max5h=MAX_5H, maxweek=MAX_WEEK, stop5h=STOP_5H):
    """The verdict line. A 5h STOP outranks everything: live workers would keep spending while the
    operator is asked. Otherwise a weekly limit outranks the 5h one: holding for 5h doesn't help a
    spent week. A HOLD with no reset time is an ASK: the supervisor would have nothing to wake at."""
    h5 = [l for l in lims if l[0] == "5h"]
    if h5 and h5[0][1] >= stop5h:   # stops even with no reset time
        _, p, at, _ = h5[0]
        return f"USAGE STOP until={at.isoformat() if at else '?'} reason=5h-at-{p:.0f}%"
    week = [l for l in lims if l[3] and l[1] >= maxweek]
    if week:
        n, p, at, _ = max(week, key=lambda l: l[1])
        return f"USAGE ASK until={at.isoformat() if at else '?'} reason={n.replace(' ', '-')}-at-{p:.0f}%"
    if not h5:
        return "USAGE UNKNOWN reason=no-5h-window-in-answer"
    _, p, at, _ = h5[0]
    if p >= max5h:
        return (f"USAGE HOLD until={at.isoformat()} reason=5h-at-{p:.0f}%" if at
                else f"USAGE ASK until=? reason=5h-at-{p:.0f}%-no-reset-time")
    return "USAGE OK"


def run_gates(run_dir):
    """(stop, hold) for a run: plan.json's usage_stop_5h, hold 5 points under it; the defaults without it.
    A missing plan or a value outside 1-100 raises, so a typo never silently means "no stop"."""
    with open(os.path.join(run_dir, "plan.json")) as f:
        plan = json.load(f)
    if not isinstance(plan, dict):
        raise ValueError("plan.json is not an object")
    stop = plan.get("usage_stop_5h")
    if stop is None:
        return STOP_5H, MAX_5H
    if isinstance(stop, bool) or not isinstance(stop, (int, float)) or not 1 <= stop <= 100:
        raise ValueError(f"usage_stop_5h={stop!r}")
    return float(stop), min(float(MAX_5H), max(stop - 5, 0.0))


def main(argv):
    as_json = "--json" in argv
    def opt(flag, default):
        if flag not in argv:
            return default
        return float(argv[argv.index(flag) + 1])
    try:
        stop_default, max_default = STOP_5H, MAX_5H
        if "--run" in argv:
            stop_default, max_default = run_gates(argv[argv.index("--run") + 1])
        max5h, maxweek = opt("--max-5h", max_default), opt("--max-week", MAX_WEEK)
        stop5h = opt("--stop-5h", stop_default)
    except (IndexError, ValueError, OSError):
        print("USAGE UNKNOWN reason=bad-arguments (--run <dir> --max-5h N --stop-5h N --max-week N)")
        return 3
    unknown = [a for a in argv if a.startswith("-") and a not in ("--json", "--run", "--max-5h", "--stop-5h", "--max-week")]
    if unknown:
        print(f"USAGE UNKNOWN reason=unknown-flag:{unknown[0]}")
        return 3

    tok = token()
    if not tok:
        print("USAGE UNKNOWN reason=no-oauth-credential")
        return 3
    try:
        data = fetch(tok)
    except urllib.error.HTTPError as e:
        why = {401: "token-expired-any-claude-turn-refreshes-it", 403: "token-lacks-user:profile-scope"}
        print(f"USAGE UNKNOWN reason={why.get(e.code, f'http-{e.code}')}")
        return 3
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"USAGE UNKNOWN reason=unreachable:{type(e).__name__}")
        return 3

    now = datetime.now(timezone.utc)
    lims = limits(data)
    v = verdict(lims, max5h, maxweek, stop5h)
    if as_json:
        print(json.dumps({"limits": [{"name": n, "used": p, "left": 100 - p,
                                      "resets_at": at.isoformat() if at else None}
                                     for n, p, at, _ in lims], "verdict": v}))
    else:
        for n, p, at, _ in lims:
            print(f"{n}: {p:.0f}% used, {100 - p:.0f}% left, resets "
                  f"{at.strftime('%Y-%m-%d %H:%MZ') if at else '?'} (in {_in(at, now)})")
        print(v)
    return 3 if v.startswith("USAGE UNKNOWN") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
