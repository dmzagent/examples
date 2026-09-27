"""The dmz command line against the fake platform over HTTP."""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dmz.cli import main  # noqa: E402
from fake_platform import FakePlatform, FakePlatformHTTP  # noqa: E402


def run(*argv: str, stdin: str | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--color", "never", *argv])
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


class CliCase(unittest.TestCase):
    role = "tenant_admin"

    @classmethod
    def setUpClass(cls):
        cls.fake = FakePlatform(role=cls.role, installed_canons=["cn_core_concordex", "cn_seed_openai_agent_safety"])
        cls.http = FakePlatformHTTP(cls.fake).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env_backup = dict(os.environ)
        for var in ("DMZAGENT_API_KEY", "DMZAGENT_APP_KEY", "DMZAGENT_BASE_URL", "DMZAGENT_DIVISION_ID",
                    "DMZAGENT_WORKSPACE_ID", "DMZAGENT_ENV_FILE", "DMZ_PROFILE"):
            os.environ.pop(var, None)
        os.environ["XDG_CONFIG_HOME"] = str(Path(cls.tmp.name) / "config")
        os.environ["DMZAGENT_API_KEY"] = "ck_test_key_0123456789"
        os.environ["DMZAGENT_BASE_URL"] = cls.http.base_url
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        cls.http.stop()
        os.environ.clear()
        os.environ.update(cls.env_backup)
        cls.tmp.cleanup()


class TestIdentityAndDoctor(CliCase):
    def test_whoami_names_the_workspace_role_and_division(self):
        code, out, _ = run("whoami", "--json")
        self.assertEqual(code, 0)
        who = json.loads(out)
        self.assertEqual((who["workspace_id"], who["role"], who["division_id"]), ("ws_test", "tenant_admin", "dv_test"))
        self.assertEqual(who["key"], "ck_test_…")
        self.assertNotIn("0123456789", out)

    def test_doctor_reports_each_check(self):
        code, out, _ = run("doctor", "--json")
        self.assertEqual(code, 0)
        checks = {r["check"]: r for r in json.loads(out)}
        self.assertEqual(checks["platform"]["status"], "ok")
        self.assertEqual(checks["role"]["status"], "ok")
        self.assertIn("cn_seed_openai_agent_safety", checks["canons"]["detail"])
        self.assertEqual(checks["mcp hosts"]["status"], "warn")   # protocol 1.0 needs the bridge

    def test_a_rejected_key_is_explained(self):
        os.environ["DMZAGENT_API_KEY"] = "ck_wrong"
        self.fake.valid_keys = {"ck_test_key_0123456789"}
        try:
            code, _, err = run("whoami")
        finally:
            os.environ["DMZAGENT_API_KEY"] = "ck_test_key_0123456789"
            self.fake.valid_keys = None
        self.assertEqual(code, 3)
        self.assertIn("rejected", err)

    def test_no_key_is_a_usage_error_with_a_hint(self):
        saved = os.environ.pop("DMZAGENT_API_KEY")
        try:
            code, _, err = run("whoami")
        finally:
            os.environ["DMZAGENT_API_KEY"] = saved
        self.assertEqual(code, 2)
        self.assertIn("dmz auth set", err)

    def test_an_unreachable_endpoint_is_exit_4(self):
        code, _, err = run("--base-url", "http://127.0.0.1:9", "whoami")
        self.assertEqual(code, 4)
        self.assertIn("could not reach", err)


class TestAuthProfiles(CliCase):
    def test_a_key_pasted_on_stdin_is_saved_read_only_and_used(self):
        code, out, _ = run("--profile", "lab", "auth", "set", stdin="ck_saved_key_abcdefghij\n")
        self.assertEqual(code, 0)
        self.assertIn("ck_saved…", out)          # the first eight characters, never the secret
        self.assertNotIn("abcdefghij", out)
        path = Path(os.environ["XDG_CONFIG_HOME"]) / "dmz" / "credentials.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        code, out, _ = run("auth", "status")
        self.assertIn("lab", out)
        self.assertNotIn("abcdefghij", out)
        saved = os.environ.pop("DMZAGENT_API_KEY")
        try:
            code, out, _ = run("--profile", "lab", "whoami", "--json")
            self.assertEqual(code, 0)
            self.assertIn("profile 'lab'", json.loads(out)["key_source"])
        finally:
            os.environ["DMZAGENT_API_KEY"] = saved
        code, out, _ = run("--profile", "lab", "auth", "clear")
        self.assertIn("forgot", out)

    def test_something_that_is_not_a_key_is_refused(self):
        code, _, err = run("auth", "set", stdin="hunter2\n")
        self.assertEqual(code, 2)
        self.assertIn("nothing saved", err)


MANIFEST = {
    "apiVersion": "dmzagent.com/v1", "kind": "SolutionManifest",
    "metadata": {"name": "harbor", "vendor": "${DMZAGENT_VENDOR}", "version": 1},
    "spec": {
        "divisions": [{"id": "main", "division_id": "${DMZAGENT_DIVISION_ID}", "config": {"enforcement_posture": "${posture:-enforce}"}}],
        "corpora": [{"id": "support-corpus", "reasoning_canons": ["library/cn_seed_openai_agent_safety@latest"]}],
        "workspaces": [{"id": "support", "workspace_id": "${DMZAGENT_WORKSPACE_ID}", "division": "main",
                        "engine": "reasoning", "corpus": "support-corpus"}],
        "circuit_breaker_policies": [{"id": "block-pii", "workspace": "support", "name": "Block on PII leak",
                                      "rules": [{"tag": "rt_agent_pii_leak_v1", "op": ">=", "value": 0.6}], "action": "block"}],
        "policies": [{"id": "hold-misuse", "workspace": "support", "name": "Hold on tool misuse",
                      "when": [{"kind": "strength", "label": "rt_agent_tool_misuse_v1", "op": ">=", "threshold": 0.5}],
                      "lane": "enforce", "level": "hold"}],
        "chatbots": [{"id": "support-bot", "workspace": "support", "agent_name": "Harbor Supply support",
                      "site_domain": "support.harbor.example", "protected_action": "refund.issue",
                      "system_prompt": "Be brief."}],
        "roles": [{"principal": "auditor@harbor.example", "role": "auditor", "scope": "main"}],
        "expectations": [{"id": "misuse-holds", "workspace": "support", "labels": {"rt_agent_tool_misuse_v1": 0.7},
                          "enforce": "hold"}],
        "deployment": {"target": "cloud", "license": "${ACME_LICENSE_KEY}"},
    },
}


class TestManifestWheel(CliCase):
    def setUp(self):
        self.fake.stacks.clear()
        self.fake.drift = []
        self.fake.cb_policies.clear()
        self.fake.policies.clear()
        self.fake.chatbots.clear()
        Path("proj").mkdir(exist_ok=True)
        Path("proj/solution.json").write_text(json.dumps(MANIFEST))

    def test_init_writes_a_manifest_with_placeholders(self):
        code, out, _ = run("init", "mcp", "--dir", "fresh", "--name", "Harbor agent")
        self.assertEqual(code, 0)
        text = Path("fresh/solution.yaml").read_text()
        self.assertIn("kind: SolutionManifest", text)
        self.assertIn("${DMZAGENT_WORKSPACE_ID}", text)
        self.assertIn("role: auditor", text)
        code, _, err = run("init", "mcp", "--dir", "fresh")
        self.assertEqual(code, 2, err)

    def test_placeholders_are_filled_from_the_key_and_vars_only(self):
        code, out, _ = run("validate", "proj/solution.json", "--var", "posture=observe")
        self.assertEqual(code, 0, out)
        self.assertIn("valid", out)
        sent = json.loads(self.fake.calls_bodies[-1]["manifest"]) if hasattr(self.fake, "calls_bodies") else None
        # the fake received the substituted text: adoption ids and the var filled, the secret left as a reference
        received = self.fake.last_manifest
        self.assertEqual(received["spec"]["workspaces"][0]["workspace_id"], "ws_test")
        self.assertEqual(received["spec"]["divisions"][0]["division_id"], "dv_test")
        self.assertEqual(received["spec"]["divisions"][0]["config"]["enforcement_posture"], "observe")
        self.assertEqual(received["spec"]["deployment"]["license"], "${ACME_LICENSE_KEY}")
        self.assertEqual(received["metadata"]["vendor"], "vd_1")

    def test_validate_reports_issues_and_guardrails(self):
        m = json.loads(json.dumps(MANIFEST))
        m["spec"]["chatbots"][0].pop("site_domain")
        m["spec"]["roles"] = []
        Path("proj/bad.json").write_text(json.dumps(m))
        code, out, _ = run("validate", "proj/bad.json")
        self.assertEqual(code, 1)
        self.assertIn("site_domain is required", out)
        m["spec"]["chatbots"][0]["site_domain"] = "x.example"
        Path("proj/bad.json").write_text(json.dumps(m))
        code, out, _ = run("validate", "proj/bad.json", "--strict")
        self.assertEqual(code, 1)
        self.assertIn("auditor", out)

    def test_plan_apply_verify_stack_and_env(self):
        code, out, _ = run("plan", "proj/solution.json")
        self.assertEqual(code, 0, out)
        self.assertIn("to add", out)
        self.assertIn("support-bot", out)
        code, out, _ = run("plan", "proj/solution.json", "--markdown")
        self.assertTrue(out.startswith("### Solution Manifest plan"))
        code, out, err = run("apply", "proj/solution.json", "--approved-by", "ravi@harbor.example", "--yes",
                             "--write-env", "proj/app/.env")
        self.assertEqual(code, 0, out + err)
        self.assertIn("applied", out)
        env = Path("proj/app/.env").read_text()
        self.assertIn("DMZAGENT_WORKSPACE_ID=ws_test", env)
        self.assertIn("DMZ_CHATBOT_SUPPORT_BOT=emb_", env)
        self.assertIn("DMZ_CIRCUIT_BREAKER_POLICY_BLOCK_PII=cbp_", env)
        self.assertEqual(stat.S_IMODE(Path("proj/app/.env").stat().st_mode), 0o600)
        # the platform now holds the policy and the chat agent
        self.assertEqual([p["name"] for p in self.fake.cb_policies.values()], ["Block on PII leak"])
        self.assertEqual(len(self.fake.chatbots), 1)
        code, out, _ = run("apply", "proj/solution.json", "--approved-by", "ravi@harbor.example", "--yes")
        self.assertIn("no change", out)
        code, out, _ = run("verify", "proj/solution.json")
        self.assertEqual(code, 0, out)
        self.assertIn("1 passed, 0 failed", out)
        code, out, _ = run("stack", "proj/solution.json")
        self.assertEqual(code, 0)
        self.assertIn("ravi@harbor.example", out)
        self.assertIn("emb_", out)
        code, out, _ = run("stacks")
        self.assertIn("harbor", out)

    def test_an_edit_plans_as_an_update(self):
        run("apply", "proj/solution.json", "--approved-by", "ravi@harbor.example", "--yes")
        m = json.loads(json.dumps(MANIFEST))
        m["spec"]["circuit_breaker_policies"][0]["rules"][0]["value"] = 0.8
        Path("proj/solution.json").write_text(json.dumps(m))
        code, out, _ = run("plan", "proj/solution.json")
        self.assertIn("~", out)
        self.assertIn("block-pii", out)
        self.assertIn("rules", out)

    def test_maker_checker_and_the_role_gate_are_explained(self):
        code, _, err = run("apply", "proj/solution.json", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("approver", err)
        self.assertIn("--approved-by", err)
        code, _, err = run("apply", "proj/solution.json", "--yes", "--applied-by", "ravi", "--approved-by", "ravi")
        self.assertEqual(code, 3)
        self.assertIn("different principal", err)
        self.fake.role = "analyst"
        try:
            code, _, err = run("apply", "proj/solution.json", "--yes", "--approved-by", "ravi")
            self.assertEqual(code, 3)
            self.assertIn("tenant_admin", err)
            self.assertEqual(run("plan", "proj/solution.json")[0], 0)      # analysts may plan
        finally:
            self.fake.role = "tenant_admin"

    def test_drift_is_reported_and_reconciled(self):
        run("apply", "proj/solution.json", "--approved-by", "ravi@harbor.example", "--yes")
        code, out, _ = run("drift", "proj/solution.json")
        self.assertEqual(code, 0)
        self.assertIn("no drift", out)
        self.fake.drift = [{"logical_id": "block-pii", "kind": "circuit_breaker_policy", "drift": "modified",
                            "changed_keys": ["action"], "desired": {"action": "block"}, "observed": {"action": "review"}}]
        code, out, _ = run("drift", "proj/solution.json")
        self.assertEqual(code, 1)
        self.assertIn("block-pii", out)
        self.assertIn("review", out)
        code, out, _ = run("drift", "proj/solution.json", "--reconcile")
        self.assertEqual(code, 0)
        self.assertIn("reconciled", out)

    def test_keys_mint_resolves_the_workspace_through_the_stack(self):
        run("apply", "proj/solution.json", "--approved-by", "ravi@harbor.example", "--yes")
        code, out, _ = run("keys", "mint", "proj/solution.json", "--workspace", "support", "--label", "site",
                           "--write-env", "proj/app/.env")
        self.assertEqual(code, 0, out)
        self.assertIn("minted", out)
        env = Path("proj/app/.env").read_text()
        self.assertIn("DMZAGENT_APP_KEY=ck_", env)
        self.assertNotIn(self.fake.minted_keys[-1]["key"], out)
        self.assertEqual(self.fake.minted_keys[-1]["workspace_id"], "ws_test")
        code, _, err = run("keys", "mint", "proj/solution.json", "--workspace", "nope", "--label", "x")
        self.assertEqual(code, 2)

    def test_destroy_removes_the_managed_resources(self):
        run("apply", "proj/solution.json", "--approved-by", "ravi@harbor.example", "--yes")
        code, out, _ = run("destroy", "proj/solution.json", "--approved-by", "ravi@harbor.example", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.cb_policies, {})
        self.assertEqual(self.fake.chatbots, {})

    def test_init_lists_templates(self):
        code, out, _ = run("init", "--list")
        self.assertEqual(code, 0)
        for name in ("chatbot", "agent", "sdk-app", "desk", "logic", "mcp"):
            self.assertIn(name, out)


class TestOperations(CliCase):
    def test_check_hold_check_release(self):
        code, out, _ = run("check", "customer:alice")
        self.assertEqual(code, 0)
        self.assertIn("subject:dv_test:customer:alice", out)
        code, out, _ = run("hold", "customer:alice", "--reason", "chargeback dispute")
        self.assertEqual(code, 0, out)
        self.assertIn("held subject:dv_test:customer:alice → hold", out)
        code, out, _ = run("check", "customer:alice")
        self.assertEqual(code, 1)
        self.assertIn("manual override", out)
        code, out, _ = run("states")
        self.assertIn("hold", out)
        code, out, _ = run("decisions", "--json")
        rows = json.loads(out)
        self.assertEqual((rows[0]["state_before"], rows[0]["state_after"]), ("closed", "hold"))
        code, out, _ = run("release", "customer:alice", "--reason", "resolved")
        self.assertIn("released", out)
        self.assertEqual(run("check", "customer:alice")[0], 0)

    def test_reviews_are_listed_claimed_and_resolved(self):
        self.fake.open_review("subject:dv_test:ticket:payments-agent")
        code, out, _ = run("reviews")
        self.assertEqual(code, 0)
        self.assertIn("rv_1", out)
        self.assertIn("rt_agent_tool_misuse_v1", out)
        code, out, _ = run("reviews", "claim", "rv_1")
        self.assertIn("claimed", out)
        code, out, _ = run("reviews", "resolve", "rv_1", "--decision", "accepted")
        self.assertIn("accepted", out)
        code, out, _ = run("reviews", "--status", "all", "--json")
        self.assertEqual(json.loads(out)[0]["status"], "accepted")
        code, _, err = run("reviews", "resolve")
        self.assertEqual(code, 2)

    def test_watch_once_prints_a_snapshot(self):
        code, out, _ = run("watch", "--once")
        self.assertEqual(code, 0)
        self.assertIn("breakers", out)
        self.assertIn("open reviews", out)


class TestMcpCommands(CliCase):
    def test_tools_and_resources_are_listed(self):
        code, out, _ = run("mcp", "tools")
        self.assertEqual(code, 0)
        self.assertIn("enforce_covenant", out)
        self.assertIn("write", out)
        code, out, _ = run("mcp", "resources", "--json")
        self.assertEqual([r["uri"] for r in json.loads(out)][1], "concordia:/workspace/canons")

    def test_enforce_exit_codes_follow_the_verdict(self):
        code, out, _ = run("mcp", "enforce", "customer:bob", "issue_refund", "--payload", '{"amount": 50}')
        self.assertEqual(code, 0)
        self.assertIn("allow", out)
        self.fake.transition("subject:dv_test:customer:bob", "hold", "manual override by apikey:ak_1: dispute", manual=True)
        code, out, _ = run("mcp", "enforce", "customer:bob", "issue_refund")
        self.assertEqual(code, 3)
        self.assertIn("block", out)
        self.fake.transition("subject:dv_test:customer:bob", "half_open", "cooling off")
        code, out, _ = run("mcp", "enforce", "customer:bob", "issue_refund")
        self.assertEqual(code, 1)
        self.assertIn("review", out)

    def test_record_call_and_read(self):
        code, out, _ = run("mcp", "record", "customer:bob", "refund_issued", "--payload", '{"amount": 50}', "--actor", "human")
        self.assertEqual(code, 0)
        self.assertIn("recorded refund_issued", out)
        code, out, _ = run("mcp", "call", "query_corpus", "--arg", "query=agent", "--arg", "limit=5")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["matches"][0]["canon_id"], "cn_seed_openai_agent_safety")
        self.assertEqual(self.fake.mcp_calls[-1][1]["limit"], 5)      # a JSON-looking value is passed typed
        code, out, _ = run("mcp", "read", "concordia:/workspace/recent-ledger", "--limit", "1")
        self.assertEqual(json.loads(out)["limit"], 1)

    def test_an_application_error_is_explained(self):
        code, _, err = run("mcp", "call", "get_subject_soul", "--arg", "subject_id=nobody:here")
        self.assertEqual(code, 3)
        self.assertIn("subject_not_found", err)
        self.assertIn("no soul snapshot", err)

    def test_ping_and_config(self):
        code, out, _ = run("mcp", "ping")
        self.assertEqual(code, 0)
        self.assertIn("concordia", out)
        code, out, _ = run("mcp", "config", "--client", "claude-desktop")
        self.assertEqual(code, 0)
        cfg = json.loads(out)["mcpServers"]["dmzagent"]
        self.assertEqual(cfg["args"][-2:], ["mcp", "bridge"])
        self.assertIn("--base-url", cfg["args"])                       # a non-default endpoint is carried
        self.assertLess(cfg["args"].index("-m"), cfg["args"].index("--base-url"))
        code, out, _ = run("mcp", "config", "--client", "claude-code")
        self.assertTrue(out.startswith("claude mcp add dmzagent"))
        code, out, _ = run("mcp", "config", "--client", "vscode")
        self.assertEqual(json.loads(out)["servers"]["dmzagent"]["type"], "stdio")


class TestCompletion(unittest.TestCase):
    def test_scripts_name_every_command(self):
        for shell in ("bash", "zsh", "fish"):
            code, out, _ = run("completion", shell)
            self.assertEqual(code, 0)
            for cmd in ("whoami", "apply", "reviews", "mcp"):
                self.assertIn(cmd, out)


if __name__ == "__main__":
    unittest.main()
