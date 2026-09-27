"""The MCP-governed agent against a scripted model and the fake platform's
MCP server: a careful model, a lazy one, a held customer, a broken tool."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli" / "tests"))

import agent as agent_module  # noqa: E402
import governance as gov_module  # noqa: E402
from dmz.mcp.client import McpClient  # noqa: E402
from fake_platform import FakePlatform, FakePlatformHTTP  # noqa: E402


class FakeOllama(BaseHTTPRequestHandler):
    """A model that follows the rules when `careful` and skips the pre-flight
    when not. Its next move depends only on the last message."""
    careful = True

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send({"models": [{"name": "llama3.1:latest"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        last = req["messages"][-1]
        msg = self.next_move(last)
        self._send({"model": "llama3.1", "message": msg, "done": True})

    @classmethod
    def next_move(cls, last: dict) -> dict:
        def call(name, **arguments):
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}
        if last["role"] == "user":
            text = last["content"].lower()
            if "status" in text:
                return call("lookup_order", order_id="HS-1002")
            if "refund" in text:
                if cls.careful:
                    return call("enforce_covenant", subject_id="customer:alice", action_kind="issue_refund",
                                action_payload={"order_id": "HS-1001", "amount": 50})
                return call("issue_refund", order_id="HS-1001", amount=50, reason="broken")
            if "export" in text:
                return call("export_customer_records", customer_id="customer:alice")   # never pre-flights this one
            if "why" in text:
                return call("get_subject_soul", subject_id="customer:alice")
            return {"role": "assistant", "content": "How can I help?"}
        result = json.loads(last["content"])
        tool = last.get("tool_name")
        if tool == "enforce_covenant":
            if result.get("verdict") == "allow":
                return call("issue_refund", order_id="HS-1001", amount=50, reason="broken")
            return {"role": "assistant", "content": f"I'm sorry, I can't do that right now: {result.get('rationale')}"}
        if tool == "issue_refund":
            if result.get("refused"):
                return {"role": "assistant", "content": "Sorry, that was refused: " + result.get("reason", "")}
            if cls.careful:
                return call("record_decision", subject_id="customer:alice", decision_kind="refund_issued",
                            payload={"order_id": "HS-1001", "amount": 50})
            return {"role": "assistant", "content": "Done: refunded."}
        if tool == "record_decision":
            return {"role": "assistant", "content": "Done: the refund is on its way."}
        if tool == "export_customer_records":
            if result.get("refused"):
                return {"role": "assistant", "content": "Sorry, that was refused: " + result.get("reason", "")}
            return {"role": "assistant", "content": f"Exported {len(result.get('records', []))} records."}
        if tool == "get_subject_soul":
            return {"role": "assistant", "content": "Soul: " + json.dumps(result)[:60]}
        if tool == "lookup_order":
            return {"role": "assistant", "content": f"Order {result.get('order_id')} ({result.get('item')}) is {result.get('status')}."}
        return {"role": "assistant", "content": "Done: " + json.dumps(result)[:60]}

    def _send(self, data):
        body = json.dumps(data).encode()
        self.send_response(200); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)


class TestAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ollama = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.ollama.serve_forever, daemon=True).start()
        cls.model = agent_module.Ollama(f"http://127.0.0.1:{cls.ollama.server_address[1]}", "llama3.1")

    @classmethod
    def tearDownClass(cls):
        cls.ollama.shutdown()

    def setUp(self):
        FakeOllama.careful = True
        self.fake = FakePlatform(role="analyst")
        self.events: list[str] = []
        self.agent = agent_module.Agent(self.model, McpClient("http://fake", "ck_app", transport=self.fake, min_interval=0),
                                        customer="customer:alice", on_event=self.events.append)

    def calls(self):
        return [name for name, _ in self.fake.mcp_calls if name != "initialize"]

    def test_platform_tools_are_offered_to_the_model_with_their_schemas(self):
        names = [t["function"]["name"] for t in self.agent.tools]
        self.assertIn("enforce_covenant", names)
        self.assertIn("issue_refund", names)
        enforce = next(t for t in self.agent.tools if t["function"]["name"] == "enforce_covenant")
        self.assertEqual(enforce["function"]["parameters"]["required"], ["subject_id", "action_kind"])

    def test_a_careful_model_preflights_acts_and_records(self):
        reply = self.agent.turn("Order HS-1001 arrived broken. Please refund it.")
        self.assertIn("on its way", reply)
        self.assertEqual(self.calls(), ["enforce_covenant", "record_decision"])
        self.assertEqual(len(self.agent.ledger), 2)
        self.assertFalse(any(line.startswith("  !") for line in self.events), self.events)

    def test_a_lazy_model_gets_the_preflight_from_the_harness(self):
        FakeOllama.careful = False
        reply = self.agent.turn("Order HS-1001 arrived broken. Please refund it.")
        self.assertIn("refunded", reply)
        self.assertEqual(self.calls(), ["enforce_covenant", "record_decision"])
        self.assertTrue(any("without a pre-flight" in line for line in self.events))
        self.assertTrue(any("harness records it" in line for line in self.events))
        args = self.fake.mcp_calls[-1][1]
        self.assertEqual((args["decision_kind"], args["outcome"]), ("refund_issued", "completed"))

    def test_a_held_customer_is_refused_and_the_refusal_is_recorded(self):
        self.fake.transition("subject:dv_test:customer:alice", "hold", "manual override by apikey:ak_1: chargeback dispute", manual=True)
        reply = self.agent.turn("Export all the records you hold about me.")
        self.assertIn("refused", reply)
        self.assertIn("chargeback dispute", reply)
        self.assertEqual(self.calls(), ["enforce_covenant", "record_decision"])
        args = self.fake.mcp_calls[-1][1]
        self.assertEqual((args["decision_kind"], args["outcome"]), ("records_exported", "rejected"))
        self.assertTrue(any("refused: verdict block" in line for line in self.events))

    def test_an_allowed_export_runs_once_per_verdict(self):
        reply = self.agent.turn("Export all the records you hold about me.")
        self.assertIn("Exported 2 records", reply)
        self.assertEqual(self.calls(), ["enforce_covenant", "record_decision"])

    def test_a_failing_platform_tool_is_reported_not_fatal(self):
        reply = self.agent.turn("why was I refused?")
        self.assertTrue(reply.startswith("Soul:"))
        self.assertIn("No soul snapshot", reply)
        self.assertTrue(any("✗ get_subject_soul" in line and "subject_not_found" in line for line in self.events), self.events)

    def test_a_status_question_needs_no_governance(self):
        reply = self.agent.turn("What's the status of order HS-1002?")
        self.assertIn("shipped", reply)
        self.assertEqual(self.calls(), [])


class TestGovernance(unittest.TestCase):
    def test_the_agent_holds_an_analyst_key_and_the_breaker_rules_exist(self):
        g = gov_module.governance
        self.assertEqual([k.env_var for k in g.by_kind("sdk_key")], ["DMZAGENT_APP_KEY"])
        self.assertEqual({p.action for p in g.by_kind("breaker_policy")}, {"block", "review"})
        self.assertEqual(g.by_kind("canon")[0].canon_id, "cn_seed_openai_agent_safety")


if __name__ == "__main__":
    unittest.main()
