"""The agent and the operator, against a fake platform on localhost.

The fake answers each step the way solution.yaml's rulebook would: a push
is blocked, curl is held and names an approval, pytest proceeds with a
positive behavior. What each test holds is in its name; the one most worth
keeping is that a held call runs only when a person approved it.
"""
from __future__ import annotations

import io
import json
import re
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import agent  # noqa: E402
import approve  # noqa: E402
from dmzagent import DMZAgent  # noqa: E402


class FakePlatform(BaseHTTPRequestHandler):
    steps: list[dict] = []
    approvals: dict[str, dict] = {}
    # What a person does with the next approval the fake parks: None leaves
    # it pending, otherwise "approved" or "declined" once it is first read.
    decide_next: str | None = None

    def log_message(self, *a):
        pass

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _authed(self):
        return self.headers.get("authorization") == "Bearer ck_app"

    def do_GET(self):
        if not self._authed():
            return self._json(401, {"detail": "not authenticated"})
        m = re.fullmatch(r"/v1/approvals/(apr_\w+)", self.path)
        if m:
            a = self.approvals.get(m.group(1))
            if a is None:
                return self._json(404, {"detail": "no such approval"})
            if a["status"] == "pending" and self.decide_next:
                a["status"] = self.decide_next
                a["decision"] = {"decision": "approve" if self.decide_next == "approved" else "decline",
                                 "actor_id": "dana", "decided_at": "2026-10-08T00:00:00Z"}
            return self._json(200, a)
        if self.path.startswith("/v1/approvals"):
            waiting = [a for a in self.approvals.values() if a["status"] == "pending"]
            return self._json(200, {"approvals": waiting, "next_cursor": None})
        if re.fullmatch(r"/v1/subjects/[^/]+/behaviors(\?.*)?", self.path):
            return self._json(200, {"behaviors": [
                {"behavior_id": "bh_1", "tag": "verified_before_claiming", "polarity": "positive",
                 "strength": 1.0, "source": "logic", "evidence": ["fr_1"], "calls": ["c1"],
                 "subject_id": agent.AGENT, "observed_at": "2026-10-08T00:00:00Z"}],
                "next_cursor": None})
        return self._json(404, {"detail": "no route"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self._authed():
            return self._json(401, {"detail": "not authenticated"})
        if self.path == "/v1/agent-stream/step":
            self.steps.append(body)
            return self._json(200, self._answer(body))
        m = re.fullmatch(r"/v1/approvals/(apr_\w+)/decision", self.path)
        if m:
            a = self.approvals[m.group(1)]
            if a["status"] != "pending":
                return self._json(409, a)
            a["status"] = "approved" if body["decision"] == "approve" else "declined"
            a["decision"] = {"decision": body["decision"], "actor_id": body["actor_id"],
                             "reason": body.get("reason"), "decided_at": "2026-10-08T00:00:00Z"}
            return self._json(200, a)
        return self._json(404, {"detail": "no route"})

    def _answer(self, step):
        answer = {"frame_id": f"fr_{len(self.steps)}", "interaction_id": step["interaction_id"],
                  "directive": "proceed", "scope": None, "reason": "", "approval_id": None,
                  "settled": True, "behaviors": [], "livemode": False}
        command = (step.get("args") or {}).get("command", "")
        if step["phase"] != "call":
            return answer
        if re.match(r"^git\s+(push|remote)\b", command):
            answer.update(directive="block", scope="interaction", reason="remote write",
                          behaviors=[{"tag": "remote_write", "polarity": "negative", "strength": 1.0,
                                      "source": "logic", "evidence": [], "calls": [step["call_id"]]}])
        elif re.match(r"^(curl|wget)\b", command):
            aid = f"apr_{len(self.approvals) + 1}"
            self.approvals[aid] = {"approval_id": aid, "status": "pending", "subject_id": agent.AGENT,
                                   "action": {"tool": step["tool"], "args": step["args"]},
                                   "reason": "unsanctioned egress", "on_expiry": "decline",
                                   "requested_at": "2026-10-08T00:00:00Z",
                                   "expires_at": "2026-10-08T01:00:00Z", "decision": None}
            answer.update(directive="hold", scope="interaction", reason="unsanctioned egress",
                          approval_id=aid)
        elif command.startswith("pytest"):
            answer["behaviors"] = [{"tag": "verified_before_claiming", "polarity": "positive",
                                    "strength": 1.0, "source": "logic", "evidence": [],
                                    "calls": [step["call_id"]]}]
        return answer


class TestGovernedSession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakePlatform)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.cx = DMZAgent(api_key="ck_app", base_url=f"http://127.0.0.1:{cls.fake.server_address[1]}")

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()

    def setUp(self):
        FakePlatform.steps = []
        FakePlatform.approvals = {}
        FakePlatform.decide_next = None

    def play(self, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            report = agent.run(self.cx, session_id="sess_t", poll=0.01, **kw)
        return {r["call_id"]: r for r in report}, out.getvalue()

    def sent(self, phase, call_id=None):
        return [s for s in FakePlatform.steps
                if s["phase"] == phase and (call_id is None or s.get("call_id") == call_id)]

    def test_the_session_states_its_intent_before_any_call(self):
        self.play(hold_seconds=0)
        first = FakePlatform.steps[0]
        self.assertEqual((first["phase"], first["intent"]["text"]), ("intent", agent.INTENT[0]))
        self.assertEqual({s["interaction_id"] for s in FakePlatform.steps}, {"sess_t"})

    def test_an_ordinary_call_runs_and_reports_its_result(self):
        report, _ = self.play(hold_seconds=0)
        self.assertTrue(report["c1"]["ran"] and report["c2"]["ran"])
        self.assertEqual([s["status"] for s in self.sent("result", "c1")], ["ok"])

    def test_a_blocked_call_does_not_run_and_the_session_carries_on(self):
        report, out = self.play(hold_seconds=0)
        self.assertEqual((report["c5"]["directive"], report["c5"]["ran"]), ("block", False))
        [refusal] = self.sent("result", "c5")
        self.assertEqual((refusal["status"], refusal["refused_by"]), ("refused", "governor"))
        self.assertIn("observed: remote_write (negative)", out)

    def test_the_harness_refuses_before_dmzagent_is_asked_and_says_so(self):
        report, _ = self.play(hold_seconds=0)
        self.assertEqual(self.sent("call", "c3"), [])
        [refusal] = self.sent("result", "c3")
        self.assertEqual((refusal["status"], refusal["refused_by"]), ("refused", "harness"))
        self.assertFalse(report["c3"]["ran"])

    def test_a_held_call_runs_when_a_person_approves_it(self):
        FakePlatform.decide_next = "approved"
        report, out = self.play(hold_seconds=5)
        self.assertEqual((report["c4"]["directive"], report["c4"]["ran"]), ("hold", True))
        self.assertEqual([s["status"] for s in self.sent("result", "c4")], ["ok"])
        self.assertIn("approval approved by dana", out)

    def test_a_held_call_declined_is_refused_by_the_governor(self):
        FakePlatform.decide_next = "declined"
        report, _ = self.play(hold_seconds=5)
        self.assertFalse(report["c4"]["ran"])
        self.assertEqual(self.sent("result", "c4")[0]["refused_by"], "governor")

    def test_a_held_call_nobody_decides_is_not_run(self):
        report, out = self.play(hold_seconds=0.05)
        self.assertFalse(report["c4"]["ran"])
        self.assertIn("no decision in time", out)

    def test_a_hold_that_names_no_approval_is_a_block(self):
        from dmzagent import StepResult
        r = StepResult(frame_id="f", interaction_id="s", directive="hold", approval_id=None)
        self.assertFalse(agent.wait_on(self.cx, r, 5, 0.01, lambda *_: None))

    def test_the_operator_sees_the_held_call_and_decides_it_with_a_name(self):
        self.play(hold_seconds=0)
        out = io.StringIO()
        with redirect_stdout(out):
            [a] = approve.pending(self.cx)
            status = approve.decide(self.cx, a.approval_id, "approve", "dana", "verified the package")
            again = approve.decide(self.cx, a.approval_id, "decline", "ravi", None)
        self.assertIn("curl -sS https://pypi.org", out.getvalue())
        self.assertEqual((status, again), ("approved", "approved"))
        self.assertEqual(FakePlatform.approvals[a.approval_id]["decision"]["actor_id"], "dana")
        self.assertIn("was already approved", out.getvalue())

    def test_the_conduct_record_reads_back(self):
        out = io.StringIO()
        with redirect_stdout(out):
            [b] = approve.behaviors(self.cx, agent.AGENT)
        self.assertEqual((b.tag, b.polarity, b.calls), ("verified_before_claiming", "positive", ["c1"]))


if __name__ == "__main__":
    unittest.main()
