"""The governed assistant, against a fake Ollama and a fake platform.

Uses the real dmzagent SDK against the fake platform, so what is tested is
the wire usage an operator would see: the conversation events, the check
before a sensitive tool, and the outcome wait."""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from dmzagent import DMZAgent  # noqa: E402

import chat  # noqa: E402


class FakeOllama(BaseHTTPRequestHandler):
    script: list[dict] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": "llama3.1:latest"}]}).encode()
        self.send_response(200); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(n))
        # Answer with a tool call if the last message asks for a refund and no tool result is there yet.
        last = req["messages"][-1]
        if last["role"] == "user" and "refund" in last["content"].lower():
            msg = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "issue_refund",
                   "arguments": {"order_id": "HS-1001", "amount": 50, "reason": "damaged"}}}]}
        elif last["role"] == "user" and "status" in last["content"].lower():
            msg = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "lookup_order",
                   "arguments": {"order_id": "HS-1002"}}}]}
        elif last["role"] == "tool":
            result = json.loads(last["content"])
            msg = {"role": "assistant", "content": ("Sorry, that was refused: " + result.get("reason", "")) if result.get("refused")
                   else "Done: " + json.dumps(result)}
        else:
            msg = {"role": "assistant", "content": "Hello from the fake model."}
        body = json.dumps({"model": "llama3.1", "message": msg, "done": True}).encode()
        self.send_response(200); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)


class FakePlatform(BaseHTTPRequestHandler):
    events: list[dict] = []
    breaker = {"state": "closed", "allow": True, "held": False, "warning": False, "reason": "no cached state"}
    overrides: list[dict] = []

    def log_message(self, *a):
        pass

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/frames/"):
            fid = self.path.split("/")[3]
            return self._json(200, {"frame_id": fid, "outcome": "applied", "division_id": "dv_1",
                                    "workspace_ids": ["ws_1"], "summary": {"complete": True, "trace_count": 1, "workspace_count": 1},
                                    "tags_fired": [{"tag_id": "rt_owasp_llm06_excessive_agency_2025_v1", "strength": 0.61}],
                                    "reasoning": [{"workspace_id": "ws_1", "outcome": "applied"}]})
        if self.path.startswith("/v1/reviews"):
            return self._json(200, {"reviews": []})
        if self.path.startswith("/v1/cb/decisions"):
            return self._json(200, {"decisions": []})
        return self._json(404, {"detail": "no route"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.headers.get("authorization") != "Bearer ck_app":
            return self._json(401, {"detail": "not authenticated"})
        if self.path == "/v1/agent-stream/event":
            self.events.append(body)
            fid = f"frame_{len(self.events)}"
            return self._json(200, {"interaction_id": "int_1", "subjects": [s["subject_id"] for s in body.get("subjects", [])],
                                    "frame_id": fid, "accepted": True, "n_workspaces": 1,
                                    "follow_my_data": f"/v1/frames/{fid}/story", "livemode": True})
        if self.path == "/v1/cb/check":
            if self.breaker.get("state") == "broken":
                return self._json(500, {"detail": "boom"})
            b = dict(self.breaker)
            b.update({"fired_policies": [], "anchor": None, "checked_at": "", "latency_ms": 1.0, "route_latency_ms": 1.0})
            return self._json(200, b)
        if self.path in ("/v1/cb/hold", "/v1/cb/release"):
            self.overrides.append({"path": self.path, **body})
            return self._json(200, {"state": {"state": "hold" if self.path.endswith("hold") else "closed"}})
        return self._json(404, {"detail": "no route"})


class TestGovernedAssistant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ollama_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        cls.platform_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakePlatform)
        for srv in (cls.ollama_srv, cls.platform_srv):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.platform_srv.server_address[1]}"
        cls.cfg = {"DMZAGENT_BASE_URL": base, "DMZAGENT_APP_KEY": "ck_app", "DMZAGENT_WORKSPACE_ID": "ws_1",
                   "DMZAGENT_DIVISION_ID": "dv_1", "BOT_SUBJECT_SLUG": "harbor-assistant"}
        cls.ollama = chat.Ollama(f"http://127.0.0.1:{cls.ollama_srv.server_address[1]}", "llama3.1")

    @classmethod
    def tearDownClass(cls):
        cls.ollama_srv.shutdown(); cls.platform_srv.shutdown()

    def setUp(self):
        FakePlatform.events.clear(); FakePlatform.overrides.clear()
        FakePlatform.breaker = {"state": "closed", "allow": True, "held": False, "warning": False, "reason": "no cached state"}
        self.dmz = DMZAgent(api_key="ck_app", base_url=self.cfg["DMZAGENT_BASE_URL"])
        self.assistant = chat.GovernedAssistant(self.cfg, self.ollama, self.dmz)

    def _wait_outcomes(self):
        for _ in range(50):
            if all(t.get("outcome") for t in self.assistant.turns if t.get("frame_id")):
                return
            time.sleep(0.05)

    def test_subjects_are_canonical_and_division_scoped(self):
        self.assertEqual(self.assistant.bot, "subject:dv_1:chat:harbor-assistant")
        conv = self.assistant.conversation("c1")
        self.assertEqual(conv["customer"], "subject:dv_1:chat:visitor-c1")

    def test_a_plain_turn_records_both_utterances_and_the_tool(self):
        out = self.assistant.turn("c1", "What is the status of HS-1002?")
        kinds = [e["kind"] for e in FakePlatform.events]
        self.assertEqual(kinds, ["subject_says", "tool_call", "tool_result", "subject_says"])
        self.assertEqual(FakePlatform.events[0]["speaker_subject_id"], "subject:dv_1:chat:visitor-c1")
        self.assertEqual(FakePlatform.events[0]["agent_subject_id"], "subject:dv_1:chat:harbor-assistant")
        self.assertEqual(FakePlatform.events[1]["payload"]["tool"], "lookup_order")
        self.assertFalse(out["events"][0]["governed"])
        self.assertIn("shipped", out["reply"])
        self.assertNotIn("card_last4", json.dumps(out["events"][0]["result"]))

    def test_a_refund_is_checked_and_runs_when_the_breaker_is_closed(self):
        out = self.assistant.turn("c2", "Please refund $50 on HS-1001, it arrived damaged")
        ev = out["events"][0]
        self.assertTrue(ev["governed"])
        self.assertEqual(ev["governance"]["state"], "closed")
        self.assertEqual(ev["result"]["refunded"], 50.0)
        self.assertIn("Done", out["reply"])

    def test_a_refund_is_refused_when_the_breaker_is_held(self):
        FakePlatform.breaker = {"state": "hold", "allow": False, "held": True, "warning": False,
                                "reason": "manual override by an operator: drill"}
        out = self.assistant.turn("c3", "Refund $50 on HS-1001")
        ev = out["events"][0]
        self.assertTrue(ev["result"]["refused"])
        self.assertEqual(ev["governance"]["state"], "hold")
        self.assertIn("refused", out["reply"].lower())
        # The refusal itself is on the record: the tool_result event carries it.
        results = [e for e in FakePlatform.events if e["kind"] == "tool_result"]
        self.assertTrue(results[0]["payload"]["result"]["refused"])

    def test_half_open_runs_the_tool_but_flags_it(self):
        FakePlatform.breaker = {"state": "half_open", "allow": True, "held": False, "warning": True,
                                "reason": "Flag suspected prompt injection"}
        out = self.assistant.turn("c4", "Refund $50 on HS-1001")
        self.assertEqual(out["events"][0]["result"]["flagged"], "Flag suspected prompt injection")

    def test_a_failed_check_refuses_a_governed_tool(self):
        FakePlatform.breaker = {"state": "broken"}
        out = self.assistant.turn("c6", "Refund $50 on HS-1001")
        ev = out["events"][0]
        self.assertTrue(ev["result"]["refused"])
        self.assertTrue(ev["governance"]["unavailable"])
        self.assertIn("could not be checked", ev["governance"]["reason"])

    def test_outcomes_arrive_with_their_tags(self):
        self.assistant.turn("c5", "hello")
        self._wait_outcomes()
        customer_turn = self.assistant.turns[0]
        self.assertEqual(customer_turn["outcome"], "applied")
        self.assertEqual(customer_turn["tags"][0]["tag"], "rt_owasp_llm06_excessive_agency_2025_v1")

    def test_panel_state_and_drill(self):
        state = self.assistant.state()
        self.assertEqual(state["breaker"]["state"], "closed")
        self.assistant.override("hold", "drill")
        self.assertEqual(FakePlatform.overrides[0]["subject_id"], "subject:dv_1:chat:harbor-assistant")


if __name__ == "__main__":
    unittest.main()
