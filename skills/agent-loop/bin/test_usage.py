"""Tests for usage.py: parsing the /api/oauth/usage answer and the verdict line."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage  # noqa: E402

R5 = "2026-11-12T11:30:00.338694+00:00"
R7 = "2026-11-19T11:00:00+00:00"


def answer(h5=14.0, week=1.0, scoped=0):
    return {"five_hour": {"utilization": h5, "resets_at": R5},
            "seven_day": {"utilization": week, "resets_at": R7},
            "seven_day_opus": None,
            "limits": [{"kind": "session", "percent": int(h5), "resets_at": R5},
                       {"kind": "weekly_scoped", "percent": scoped, "resets_at": R7,
                        "scope": {"model": {"display_name": "Fable"}}}]}


class Limits(unittest.TestCase):
    def test_parses_5h_week_and_scoped(self):
        lims = usage.limits(answer())
        self.assertEqual([(n, p, w) for n, p, _, w in lims],
                         [("5h", 14.0, False), ("week", 1.0, True), ("Fable week", 0.0, True)])
        self.assertEqual(lims[0][2], datetime(2026, 11, 12, 11, 30, 0, 338694, tzinfo=timezone.utc))

    def test_missing_fields_are_skipped(self):
        self.assertEqual(usage.limits({"five_hour": None, "seven_day": {"utilization": None}}), [])


class Verdict(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(usage.verdict(usage.limits(answer())), "USAGE OK")

    def test_5h_hold_at_threshold(self):
        self.assertEqual(usage.verdict(usage.limits(answer(h5=89))), "USAGE OK")
        self.assertEqual(usage.verdict(usage.limits(answer(h5=90))),
                         f"USAGE HOLD until={R5} reason=5h-at-90%")

    def test_5h_stop_at_95(self):
        self.assertEqual(usage.verdict(usage.limits(answer(h5=95))),
                         f"USAGE STOP until={R5} reason=5h-at-95%")
        a = answer(h5=97)
        a["five_hour"]["resets_at"] = "not-a-time"
        self.assertEqual(usage.verdict(usage.limits(a)), "USAGE STOP until=? reason=5h-at-97%")

    def test_week_outranks_5h_hold(self):
        v = usage.verdict(usage.limits(answer(h5=92, week=92)))
        self.assertEqual(v, f"USAGE ASK until={R7} reason=week-at-92%")

    def test_5h_stop_outranks_week(self):
        v = usage.verdict(usage.limits(answer(h5=99, week=92)))
        self.assertEqual(v, f"USAGE STOP until={R5} reason=5h-at-99%")

    def test_scoped_week_asks(self):
        v = usage.verdict(usage.limits(answer(scoped=95)))
        self.assertEqual(v, f"USAGE ASK until={R7} reason=Fable-week-at-95%")

    def test_thresholds_are_settable(self):
        self.assertTrue(usage.verdict(usage.limits(answer(h5=50)), max5h=50).startswith("USAGE HOLD"))
        self.assertTrue(usage.verdict(usage.limits(answer(h5=60)), max5h=50, stop5h=60).startswith("USAGE STOP"))

    def test_no_5h_is_unknown(self):
        self.assertTrue(usage.verdict([]).startswith("USAGE UNKNOWN"))

    def test_spent_week_asks_even_without_5h(self):
        a = answer(week=99)
        a["five_hour"] = None
        self.assertTrue(usage.verdict(usage.limits(a)).startswith("USAGE ASK"))

    def test_hold_without_reset_time_asks(self):
        a = answer(h5=90)
        a["five_hour"]["resets_at"] = "not-a-time"
        self.assertEqual(usage.verdict(usage.limits(a)), "USAGE ASK until=? reason=5h-at-90%-no-reset-time")


class Args(unittest.TestCase):
    def run_main(self, argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = usage.main(argv)
        return rc, buf.getvalue()

    def test_bad_arguments_are_unknown_not_a_traceback(self):
        for argv in (["--max-5h"], ["--max-5h", "abc"], ["--max-5h=50"]):
            rc, out = self.run_main(argv)
            self.assertEqual(rc, 3, argv)
            self.assertTrue(out.startswith("USAGE UNKNOWN"), argv)


class RunGates(unittest.TestCase):
    def gates(self, plan):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            if plan is not None:
                with open(os.path.join(d, "plan.json"), "w") as f:
                    json.dump(plan, f)
            return usage.run_gates(d)

    def test_plan_stop_sets_stop_and_hold_below_it(self):
        self.assertEqual(self.gates({"usage_stop_5h": 80}), (80.0, 75.0))
        lims = usage.limits(answer(h5=80))
        self.assertTrue(usage.verdict(lims, 75.0, usage.MAX_WEEK, 80.0).startswith("USAGE STOP"))

    def test_no_setting_keeps_defaults_and_hold_never_rises(self):
        self.assertEqual(self.gates({}), (usage.STOP_5H, usage.MAX_5H))
        self.assertEqual(self.gates({"usage_stop_5h": 100}), (100.0, usage.MAX_5H))

    def test_bad_value_or_missing_plan_raises(self):
        for plan in ({"usage_stop_5h": "80"}, {"usage_stop_5h": 0}, {"usage_stop_5h": True}, [], 5, None):
            with self.assertRaises((ValueError, OSError), msg=plan):
                self.gates(plan)

    def test_bad_run_is_unknown(self):
        rc, out = Args.run_main(self, ["--run", "/nonexistent-run-dir"])
        self.assertEqual((rc, out.split()[:2]), (3, ["USAGE", "UNKNOWN"]))


class NoRedirect(unittest.TestCase):
    def test_redirect_is_not_followed(self):
        """A 30x must not resend the bearer token to another host."""
        import http.server, threading, urllib.error
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{srv.server_port}/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old_url, old_env = usage.URL, dict(os.environ)
        try:
            usage.URL = f"http://127.0.0.1:{srv.server_port}/usage"
            for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
                os.environ.pop(k, None)
            with self.assertRaises(urllib.error.HTTPError):
                usage.fetch("secret")
        finally:
            usage.URL = old_url
            os.environ.clear()
            os.environ.update(old_env)
            srv.shutdown()
            srv.server_close()
        self.assertEqual([p for p, _ in hits], ["/usage"])


class Token(unittest.TestCase):
    def test_env_wins_over_credentials_file(self):
        import json, tempfile
        old = dict(os.environ)
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {"accessToken": "file"}}, f)
            try:
                os.environ["CLAUDE_CONFIG_DIR"] = d
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "env"
                self.assertEqual(usage.token(), "env")
                del os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
                self.assertEqual(usage.token(), "file")
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_falls_back_to_env_without_credentials_file(self):
        old = dict(os.environ)
        try:
            os.environ["CLAUDE_CONFIG_DIR"] = "/nonexistent"
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "t"
            self.assertEqual(usage.token(), "t")
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
