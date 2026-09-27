"""The stdio bridge: in process, as a subprocess speaking standard MCP, and
through the official MCP Python SDK when it is installed."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dmz.mcp.bridge import LATEST_VERSION, Bridge  # noqa: E402
from dmz.mcp.client import McpClient  # noqa: E402
from fake_platform import FakePlatform, FakePlatformHTTP  # noqa: E402

CLI_DIR = str(Path(__file__).resolve().parents[1])


class TestBridgeInProcess(unittest.TestCase):
    def setUp(self):
        self.fake = FakePlatform(role="analyst")
        self.bridge = Bridge(McpClient("http://fake", "ck_test", transport=self.fake, min_interval=0))

    def rpc(self, method, params=None, jid=1):
        return self.bridge.handle({"jsonrpc": "2.0", "id": jid, "method": method, "params": params or {}})

    def test_initialize_negotiates_a_standard_version(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(r["result"]["serverInfo"]["name"], "dmzagent")
        self.assertIn("ws_test", r["result"]["instructions"])
        r = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(r["result"]["protocolVersion"], LATEST_VERSION)

    def test_notifications_get_no_reply(self):
        self.assertIsNone(self.bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertTrue(self.bridge.initialized)
        self.assertIsNone(self.bridge.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}))

    def test_tools_carry_annotations_and_calls_return_structured_content(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        self.assertTrue(by_name["query_corpus"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["enforce_covenant"]["annotations"]["readOnlyHint"])
        r = self.rpc("tools/call", {"name": "enforce_covenant", "arguments": {"subject_id": "customer:alice", "action_kind": "issue_refund"}})
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(r["result"]["structuredContent"]["verdict"], "allow")
        self.assertEqual(json.loads(r["result"]["content"][0]["text"])["verdict"], "allow")

    def test_application_errors_are_tool_results_not_protocol_errors(self):
        r = self.rpc("tools/call", {"name": "get_subject_soul", "arguments": {"subject_id": "nobody"}})
        self.assertNotIn("error", r)
        self.assertTrue(r["result"]["isError"])
        self.assertIn("subject_not_found", r["result"]["content"][0]["text"])
        self.assertIn("no soul snapshot", r["result"]["content"][0]["text"])

    def test_protocol_errors(self):
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(self.rpc("tools/call", {"arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.rpc("prompts/get", {"name": "x"})["error"]["code"], -32601)
        self.assertEqual(self.rpc("resources/read", {"uri": "concordia:/nope"})["error"]["code"], -32002)
        self.assertEqual(self.bridge.handle("junk")["error"]["code"], -32600)

    def test_resources_and_templates(self):
        r = self.rpc("resources/list")["result"]["resources"]
        self.assertEqual(r[0]["uri"], "concordia:/workspace/policies")
        r = self.rpc("resources/read", {"uri": "concordia:/workspace/canons"})["result"]["contents"][0]
        self.assertEqual(r["mimeType"], "application/json")
        self.assertIn("cn_core_concordex", r["text"])
        self.assertTrue(self.rpc("resources/templates/list")["result"]["resourceTemplates"])
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertEqual(self.rpc("prompts/list")["result"], {"prompts": []})

    def test_serve_frames_lines_and_batches(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": LATEST_VERSION}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "not json",
            json.dumps([{"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/x"}]),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
        ]
        stdin = io.BytesIO(("\n".join(lines) + "\n").encode())
        stdout = io.BytesIO()
        self.bridge.serve(stdin, stdout)
        replies = [json.loads(l) for l in stdout.getvalue().decode().splitlines()]
        self.assertEqual(replies[0]["id"], 1)
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertEqual(replies[2], [{"jsonrpc": "2.0", "id": 2, "result": {}}])
        self.assertEqual(replies[3]["id"], 3)
        self.assertEqual(len(replies), 4)


class TestBridgeSubprocess(unittest.TestCase):
    """What a host does: launch `dmz mcp bridge` and speak MCP on its stdio."""

    @classmethod
    def setUpClass(cls):
        cls.fake = FakePlatform(role="analyst")
        cls.http = FakePlatformHTTP(cls.fake).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.stop()

    def test_a_host_session_over_stdio(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DMZAGENT_")}
        env.update({"DMZAGENT_API_KEY": "ck_test", "PYTHONPATH": CLI_DIR, "XDG_CONFIG_HOME": "/nonexistent"})
        proc = subprocess.Popen([sys.executable, "-m", "dmz", "--base-url", self.http.base_url, "mcp", "bridge"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            def send(msg):
                proc.stdin.write((json.dumps(msg) + "\n").encode()); proc.stdin.flush()

            def recv():
                return json.loads(proc.stdout.readline())

            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "host", "version": "1"}}})
            init = recv()
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertIn("enforce_covenant", [t["name"] for t in recv()["result"]["tools"]])
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "record_decision", "arguments": {"subject_id": "customer:alice", "decision_kind": "refund_issued"}}})
            self.assertIn("ledger_entry_id", recv()["result"]["structuredContent"])
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.fake.mcp_calls[-1][0], "record_decision")


try:
    import mcp  # noqa: F401
    HAVE_SDK = True
except BaseException:  # the SDK, or a native dependency, is not importable here (a pyo3 panic is not an Exception)
    HAVE_SDK = False


@unittest.skipUnless(HAVE_SDK, "the official MCP Python SDK is not installed")
class TestBridgeWithOfficialSdk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakePlatform(role="analyst")
        cls.http = FakePlatformHTTP(cls.fake).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.stop()

    def test_the_sdk_client_completes_a_session(self):
        import asyncio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = {k: v for k, v in os.environ.items() if not k.startswith("DMZAGENT_")}
        env.update({"DMZAGENT_API_KEY": "ck_test", "PYTHONPATH": CLI_DIR, "XDG_CONFIG_HOME": "/nonexistent"})
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "dmz", "--base-url", self.http.base_url, "mcp", "bridge"], env=env)

        async def session():
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    init = await s.initialize()
                    tools = await s.list_tools()
                    res = await s.call_tool("enforce_covenant", {"subject_id": "customer:alice", "action_kind": "issue_refund"})
                    doc = await s.read_resource("concordia:/workspace/canons")
                    bad = await s.call_tool("get_subject_soul", {"subject_id": "nobody"})
                    return init, tools, res, doc, bad

        init, tools, res, doc, bad = asyncio.run(session())
        info = _field(init, "server_info", "serverInfo")
        self.assertEqual(info.name, "dmzagent")
        self.assertIn("enforce_covenant", [t.name for t in tools.tools])
        self.assertFalse(_field(res, "is_error", "isError"))
        self.assertEqual(_field(res, "structured_content", "structuredContent")["verdict"], "allow")
        self.assertIn("cn_core_concordex", str(doc.contents[0].text))
        self.assertTrue(_field(bad, "is_error", "isError"))


def _field(obj, *names):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    raise AttributeError(names)


if __name__ == "__main__":
    unittest.main()
