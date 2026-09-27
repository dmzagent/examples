"""The desk and the simulator, against a fake platform on localhost."""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import desk as desk_module  # noqa: E402
import simulate  # noqa: E402
from dmzagent import DMZAgent  # noqa: E402


class FakePlatform(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, dict]] = []
    breaker = {"state": "closed", "allow": True, "held": False, "warning": False, "reason": "default-allow"}

    def log_message(self, *a):
        pass

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        self.seen.append(("GET", self.path, {}))
        p = self.path
        if p.startswith("/v1/cb/states"):
            return self._json(200, {"states": [{"scope_ref": "subject:dv_1:ticket:payments-agent", "state": "hold",
                                                "reason": "Hold on tool misuse", "updated_at": "2026-09-27T10:00:00Z",
                                                "last_decision": {"fired_policies": [{"name": "Hold on tool misuse"}]}}]})
        if p.startswith("/v1/reviews"):
            return self._json(200, {"reviews": [{"review_id": "rev_1", "tag_id": "rt_agent_tool_misuse_v1",
                                                 "subject_id": "subject:dv_1:ticket:payments-agent", "level": "review",
                                                 "cb_state": "hold", "claimed_by": None}]})
        if p.startswith("/v1/cb/decisions"):
            return self._json(200, {"decisions": [{"scope_ref": "subject:dv_1:ticket:payments-agent", "state_before": "closed",
                                                   "state_after": "hold", "ledger_event_id": "e1234567-x",
                                                   "fired_policies": [{"name": "Hold on tool misuse"}]}]})
        if p.startswith("/v1/divisions/"):
            return self._json(200, {"config": {"enforcement_posture": "observe", "reasoning_mode": "per_frame"}})
        return self._json(404, {"detail": "no route"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self.seen.append(("POST", self.path, body))
        if self.headers.get("authorization") != "Bearer ck_app":
            return self._json(401, {"detail": "not authenticated"})
        if self.path == "/v1/agent-stream/event":
            fid = f"frame_{len(self.seen)}"
            return self._json(200, {"interaction_id": "int_1", "subjects": [], "frame_id": fid, "accepted": True,
                                    "n_workspaces": 1, "livemode": True})
        if self.path == "/v1/cb/check":
            b = dict(self.breaker); b.update({"fired_policies": [], "anchor": {"ledger_event_id": "anc"}, "checked_at": "",
                                              "latency_ms": 1.0, "route_latency_ms": 1.0})
            return self._json(200, b)
        if self.path.startswith("/v1/cb/"):
            return self._json(200, {"state": {"state": self.path.rsplit("/", 1)[1]}})
        if self.path.startswith("/v1/reviews/"):
            return self._json(200, {"review_id": "rev_1", "action": self.path.rsplit("/", 1)[1], "body": body})
        return self._json(404, {"detail": "no route"})


class TestDeskAndSimulator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakePlatform)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.fake.server_address[1]}"
        cls.desk = desk_module.Desk({"DMZAGENT_BASE_URL": cls.base, "DMZAGENT_APP_KEY": "ck_app",
                                     "DMZAGENT_WORKSPACE_ID": "ws_1", "DMZAGENT_DIVISION_ID": "dv_1"})
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), desk_module.make_handler(cls.desk))
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.site.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown(); cls.fake.shutdown()

    def setUp(self):
        FakePlatform.seen.clear()
        FakePlatform.breaker = {"state": "closed", "allow": True, "held": False, "warning": False, "reason": "default-allow"}

    def _post(self, path, body):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_snapshot_reads_posture_subjects_reviews_and_decisions(self):
        with urllib.request.urlopen(self.url + "/api/desk") as r:
            snap = json.loads(r.read())
        self.assertEqual(snap["posture"], "observe")
        self.assertEqual(snap["subjects"][0]["state"], "hold")
        self.assertEqual(snap["reviews"][0]["review_id"], "rev_1")
        self.assertEqual(snap["decisions"][0]["ledger_event_id"], "e1234567-x")
        with urllib.request.urlopen(self.url + "/") as r:
            self.assertNotIn("ck_app", r.read().decode())

    def test_review_actions_post_what_the_platform_expects(self):
        self._post("/api/reviews/rev_1/claim", {})
        self._post("/api/reviews/rev_1/resolve", {"status": "dismissed", "decision": "false positive"})
        self._post("/api/reviews/rev_1/release", {"reason": "verified"})
        paths = [c[1] for c in FakePlatform.seen if c[0] == "POST"]
        self.assertEqual(paths, ["/v1/reviews/rev_1/claim", "/v1/reviews/rev_1/resolve", "/v1/reviews/rev_1/release"])
        resolve = [c for c in FakePlatform.seen if c[1].endswith("/resolve")][0][2]
        self.assertEqual(resolve, {"status": "dismissed", "decision": "false positive"})
        status, data = self._post("/api/reviews/rev_1/resolve", {"status": "nope"})
        self.assertEqual(status, 400)

    def test_subject_overrides_and_remediation_hook(self):
        self._post("/api/subjects/hold", {"subject_id": "subject:dv_1:ticket:records-agent", "reason": "leak"})
        call = [c for c in FakePlatform.seen if c[1] == "/v1/cb/hold"][0][2]
        self.assertEqual(call["subject_id"], "subject:dv_1:ticket:records-agent")
        status, data = self._post("/hooks/remediate", {"action": "webhook", "subject_id": "x"})
        self.assertEqual(status, 202)
        self.assertEqual(self.desk.snapshot()["deliveries"][-1]["directive"]["action"], "webhook")

    def test_simulator_runs_the_action_when_closed_and_is_refused_when_held(self):
        cx = DMZAgent(api_key="ck_app", base_url=self.base)
        report = simulate.play(cx, "dv_1", simulate.SCRIPTS[1], wait_outcome=False)
        self.assertTrue(report["decision"]["ran"])
        kinds = [c[2].get("kind") for c in FakePlatform.seen if c[1] == "/v1/agent-stream/event"]
        self.assertEqual(kinds[-2:], ["tool_call", "tool_result"])
        self.assertEqual(report["agent"], "subject:dv_1:ticket:payments-agent")

        FakePlatform.seen.clear()
        FakePlatform.breaker = {"state": "hold", "allow": False, "held": True, "warning": False, "reason": "Hold on tool misuse"}
        report = simulate.play(cx, "dv_1", simulate.SCRIPTS[1], wait_outcome=False)
        self.assertFalse(report["decision"]["ran"])
        self.assertIn("Hold on tool misuse", report["decision"]["reason"])
        events = [c[2] for c in FakePlatform.seen if c[1] == "/v1/agent-stream/event"]
        self.assertEqual(events[-1]["kind"], "tool_result")           # the refusal is on the record
        self.assertFalse(events[-1]["payload"]["result"]["ok"])
        self.assertNotIn("tool_call", [e["kind"] for e in events])    # the action itself never happened


if __name__ == "__main__":
    unittest.main()
