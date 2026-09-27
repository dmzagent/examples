"""Offline tests: the definition compiles to the manifest the platform
expects, the local step reads only what it should, and the runner serves a
lease correctly."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import agent as agent_module  # noqa: E402
from dmz_agent import CLIENT, Agent, AgentApiError, Runner, arm_blocker  # noqa: E402


class FakeClient:
    """Stands in for AgentClient: one lease to hand out, results recorded."""

    def __init__(self, step: dict | None):
        self.step = step
        self.results: list[dict] = []
        self.sessions_closed = 0

    def open_session(self, *, label, agent_id=None):
        return {"session_id": "sess_1"}

    def next_step(self, session_id):
        step, self.step = self.step, None
        return step

    def post_result(self, session_id, lease_id, *, output=None, error=None):
        self.results.append({"lease_id": lease_id, "output": output, "error": error})
        return {"lease_id": lease_id, "status": "done" if error is None else "failed"}

    def close_session(self, session_id):
        self.sessions_closed += 1
        return {"status": "closed"}


class TestDefinition(unittest.TestCase):
    def test_compiles_to_the_dogma_shape(self):
        dogma = agent_module.agent.compile()
        self.assertEqual(set(dogma), {"charter", "privilege", "bound_canons", "installed_skills",
                                      "connector_bindings", "disposition_policy", "output_schemas", "spine"})
        nodes = dogma["spine"]["nodes"]
        self.assertEqual([n["id"] for n in nodes], ["scan-inbox", "triage", "draft-note"])
        self.assertEqual(nodes[0]["where"], "client")
        self.assertNotIn("where", nodes[1])
        self.assertEqual(nodes[1]["needs"], ["scan-inbox"])
        self.assertIn("auto-approve", nodes[1]["instruction"])
        self.assertEqual(dogma["disposition_policy"], {"writes": "human_confirm"})
        self.assertEqual(dogma["privilege"], "read_only")

    def test_step_bodies_never_appear_in_the_manifest(self):
        text = str(agent_module.agent.compile())
        self.assertNotIn("glob(", text)
        self.assertNotIn("read_text", text)

    def test_a_step_needs_a_docstring(self):
        a = Agent("x", charter="y")
        with self.assertRaises(ValueError):
            @a.step()
            def nameless():
                pass

    def test_needs_must_name_a_known_step(self):
        a = Agent("x", charter="y")
        with self.assertRaises(ValueError):
            @a.step(needs=["missing"])
            def later():
                """Later."""

    def test_scan_inbox_reads_only_metadata(self):
        out = agent_module.scan_inbox({})
        self.assertEqual(out["count"], 3)
        by_id = {i["invoice"]: i for i in out["invoices"]}
        self.assertEqual(by_id["INV-2041"]["amount"], 412.5)
        self.assertTrue(by_id["INV-2041"]["has_po"])
        self.assertFalse(by_id["INV-2043"]["has_po"])
        self.assertNotIn("Items", str(out))  # line items stay on this machine


class TestRunner(unittest.TestCase):
    def test_serves_a_leased_client_step_and_posts_the_output(self):
        fake = FakeClient({"lease_id": "lease_1", "node": {"id": "scan-inbox"}, "inputs": {}})
        with Runner(agent_module.agent, fake, poll_seconds=0.05) as runner:
            for _ in range(50):
                if runner.served:
                    break
                import time; time.sleep(0.02)
        self.assertEqual(fake.results[0]["lease_id"], "lease_1")
        self.assertEqual(fake.results[0]["output"]["output"]["count"], 3)
        self.assertEqual(fake.sessions_closed, 1)

    def test_an_unknown_step_is_refused_with_a_reason(self):
        fake = FakeClient({"lease_id": "lease_2", "node": {"id": "not-mine"}, "inputs": {}})
        runner = Runner(agent_module.agent, fake)
        runner.session_id = "sess_1"
        self.assertTrue(runner.poll_once())
        self.assertIn("no local step", fake.results[0]["error"])

    def test_a_local_exception_crosses_as_type_and_message_only(self):
        a = Agent("x", charter="y")

        @a.step(where=CLIENT)
        def explode(inputs):
            """Explode."""
            raise FileNotFoundError("/home/someone/private/ledger.xlsx")

        fake = FakeClient({"lease_id": "lease_3", "node": {"id": "explode"}, "inputs": {}})
        runner = Runner(a, fake)
        runner.session_id = "sess_1"
        runner.poll_once()
        self.assertTrue(fake.results[0]["error"].startswith("FileNotFoundError"))
        self.assertNotIn("Traceback", fake.results[0]["error"])


class TestArmGate(unittest.TestCase):
    def test_blockers_read_like_sentences(self):
        self.assertIsNone(arm_blocker({"status": "armed"}))
        self.assertIn("no dogma", arm_blocker({"status": "draft"}))
        self.assertIn("never been graded", arm_blocker({"status": "draft", "dogma_version": 1}))
        self.assertIn("changed since it was graded",
                      arm_blocker({"status": "trained", "dogma_version": 2, "grade": "S", "graded_version": 1}))
        self.assertIn("below the B", arm_blocker({"status": "trained", "dogma_version": 1, "grade": "C", "graded_version": 1}))


if __name__ == "__main__":
    unittest.main()
