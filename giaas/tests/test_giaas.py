"""Offline tests for the giaas toolkit, against the fake platform."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_platform import FakePlatform  # noqa: E402

import giaas  # noqa: E402
from giaas import (  # noqa: E402
    DeclarationError, Governance, Platform, PlatformError, RequirementUnmet, apply, connect,
    destroy, plan, presence, read_env_var, strength, verify,
)
from giaas.cli import load_governance, main  # noqa: E402


def make_gov(name: str = "test-gov", *, require: str | None = "cn_seed_openai_agent_safety") -> Governance:
    gov = Governance(name)
    gov.division_config(reasoning_mode="per_frame", enforcement_posture="enforce")
    if require:
        gov.require_canon(require, why="the tags below come from it")
    gov.breaker_policy("Block on injection", rules=[("rt_agent_prompt_injection_v1", ">=", 0.7)], action="block")
    gov.policy("Hold on scope creep", when=[strength("rt_agent_scope_creep_v1", ">=", 0.5)],
               lane="enforce", level="hold")
    gov.policy("Review scope creep", when=[presence("rt_agent_scope_creep_v1")], lane="coordinate", level="review")
    bot = gov.chatbot("Helper", site_domain="localhost", system_prompt="Be helpful.")
    gov.logic_rulebook("Thermal", slug="thermal", rulebook={"rules": [
        {"id": "over-temp", "match": {"predicate": "comparison", "field": "temp_c", "gt": 85},
         "on_true": [{"disposition": "enforce", "level": "hold", "tag": "thermal_stress"}]}]})
    gov.sdk_key("app", env_var="DMZAGENT_APP_KEY")
    gov.env("CHATBOT_EMBED_ID", bot.ref("embed_id"))
    gov.expect("scope creep holds", labels={"rt_agent_scope_creep_v1": 0.9}, enforce="hold",
               coordinate="review", policies=["Hold on scope creep"])
    return gov


class Env:
    """A temp directory holding a governance file, plus a connected context."""

    def __init__(self, fake: FakePlatform, gov: Governance):
        self.dir = Path(tempfile.mkdtemp(prefix="giaas_test_"))
        self.path = self.dir / "governance.py"
        self.path.write_text("# placeholder; the object is passed in directly\n")
        self.platform = Platform(api_key="ck_test_key", base_url="https://api.test", transport=fake,
                                 min_interval=0.0)
        self.gov = gov
        self.ctx = connect(gov, self.path, platform=self.platform, env_file=self.dir / "app.env")


class TestDeclarations(unittest.TestCase):
    def test_invalid_values_fail_where_they_are_written(self):
        gov = Governance("x")
        with self.assertRaises(DeclarationError):
            gov.division_config(enforcement_posture="obserev")
        with self.assertRaises(DeclarationError):
            gov.breaker_policy("p", rules=[("tag", "=>", 1)], action="block")
        with self.assertRaises(DeclarationError):
            gov.policy("p", when=[presence("t")], lane="enforce", level="review")
        with self.assertRaises(DeclarationError):
            gov.chatbot("bot")  # no site_domain, no explicit any-origin
        with self.assertRaises(DeclarationError):
            gov.sdk_key("app", env_var="not a name")

    def test_duplicate_names_are_refused(self):
        gov = Governance("x")
        gov.breaker_policy("same", rules=[("t", ">=", 1)], action="block")
        with self.assertRaises(DeclarationError):
            gov.breaker_policy("same", rules=[("t", ">=", 2)], action="review")


class TestClient(unittest.TestCase):
    def test_rate_limit_is_waited_out(self):
        fake = FakePlatform(rate_limit_first=2)
        p = Platform(api_key="ck_x", base_url="https://api.test", transport=fake, min_interval=0.0)
        who = p.whoami()
        self.assertEqual(who["workspace_id"], "ws_test")
        self.assertEqual(p.requests_made, 3)
        self.assertGreater(p.seconds_waited, 0)

    def test_errors_carry_status_and_detail(self):
        fake = FakePlatform()
        p = Platform(api_key="ck_x", base_url="https://api.test", transport=fake, min_interval=0.0)
        with self.assertRaises(PlatformError) as cm:
            p.get("/v1/nothing-here")
        self.assertEqual(cm.exception.status, 404)

    def test_key_must_look_like_a_key(self):
        with self.assertRaises(giaas.ConfigError):
            Platform(api_key="sk-not-ours", base_url="https://api.test", transport=FakePlatform())

    def test_division_lookup_closes_its_session(self):
        fake = FakePlatform()
        p = Platform(api_key="ck_x", base_url="https://api.test", transport=fake, min_interval=0.0)
        os.environ.pop("DMZAGENT_DIVISION_ID", None)
        self.assertEqual(p.division_id_for_key(), "dv_test")
        self.assertEqual(fake.sessions, [])


class TestReconcile(unittest.TestCase):
    def test_apply_then_plan_is_all_noops(self):
        fake = FakePlatform(installed_canons=["cn_core_concordex", "cn_seed_openai_agent_safety"])
        env = Env(fake, make_gov())
        changes = plan(env.gov, env.ctx)
        actions = {c.resource.ident: c.action for c in changes}
        self.assertEqual(actions["canon/cn_seed_openai_agent_safety"], "noop")
        self.assertEqual(actions["division_config/division"], "update")
        self.assertEqual(actions["breaker_policy/Block on injection"], "create")
        self.assertEqual(actions["logic_rulebook/Thermal"], "create")
        self.assertEqual(actions["sdk_key/app"], "create")
        apply(env.gov, env.ctx, changes)

        self.assertEqual(fake.config, {"reasoning_mode": "per_frame", "enforcement_posture": "enforce"})
        self.assertEqual(len(fake.cb_policies), 1)
        self.assertEqual(len(fake.policies), 2)
        self.assertEqual(len(fake.chatbots), 1)
        self.assertEqual(list(fake.logic_installs.values()), [1])
        self.assertEqual(len(fake.minted_keys), 1)

        env_file = env.dir / "app.env"
        self.assertTrue(read_env_var(env_file, "DMZAGENT_APP_KEY").startswith("ck_"))
        self.assertEqual(read_env_var(env_file, "DMZAGENT_DIVISION_ID"), "dv_test")
        self.assertTrue(read_env_var(env_file, "CHATBOT_EMBED_ID").startswith("cb_"))
        self.assertEqual(oct(env_file.stat().st_mode & 0o777), "0o600")
        state = json.loads(env.ctx.state_path.read_text())
        minted = fake.minted_keys[0]["key"]
        self.assertNotIn(minted, env.ctx.state_path.read_text())  # state never holds the secret
        self.assertNotIn(minted[:16], json.dumps(state))          # nor most of it
        self.assertEqual(state["sdk_keys"]["app"]["env_var"], "DMZAGENT_APP_KEY")

        again = connect(env.gov, env.path, platform=env.platform, env_file=env_file)
        self.assertEqual({c.action for c in plan(make_gov(), again)}, {"noop"})
        self.assertEqual(len(fake.minted_keys), 1)

    def test_requirement_stops_apply_unless_allowed(self):
        fake = FakePlatform()
        env = Env(fake, make_gov())
        changes = plan(env.gov, env.ctx)
        self.assertEqual([c.action for c in changes if c.resource.kind == "canon"], ["requires"])
        with self.assertRaises(RequirementUnmet):
            apply(env.gov, env.ctx, changes)
        self.assertEqual(len(fake.cb_policies), 0)  # nothing was created before the stop

        env.ctx.options["allow_missing_requirements"] = True
        apply(env.gov, env.ctx, plan(env.gov, env.ctx))
        self.assertEqual(len(fake.cb_policies), 1)

    def test_requirement_installs_where_the_platform_allows_it(self):
        fake = FakePlatform(corpus_install_allowed=True)
        env = Env(fake, make_gov())
        apply(env.gov, env.ctx, plan(env.gov, env.ctx))
        self.assertIn("cn_seed_openai_agent_safety", [c["canon_id"] for c in fake.canons])

    def test_drift_is_an_update_and_destroy_removes(self):
        fake = FakePlatform(installed_canons=["cn_core_concordex", "cn_seed_openai_agent_safety"])
        env = Env(fake, make_gov())
        apply(env.gov, env.ctx, plan(env.gov, env.ctx))

        drifted = make_gov()
        drifted.by_kind("breaker_policy")[0].rules[0]["value"] = 0.9
        drifted.by_kind("logic_rulebook")[0].rulebook["rules"][0]["match"]["gt"] = 80
        ctx2 = connect(drifted, env.path, platform=env.platform, env_file=env.dir / "app.env")
        actions = {c.resource.ident: c.action for c in plan(drifted, ctx2)}
        self.assertEqual(actions["breaker_policy/Block on injection"], "update")
        self.assertEqual(actions["logic_rulebook/Thermal"], "update")
        self.assertEqual(actions["policy/Hold on scope creep"], "noop")
        apply(drifted, ctx2, plan(drifted, ctx2))
        self.assertEqual(list(fake.logic_installs.values()), [2])
        self.assertEqual(list(fake.cb_policies.values())[0]["rules"][0]["value"], 0.9)

        removed = destroy(drifted, ctx2)
        self.assertEqual(fake.cb_policies, {})
        self.assertEqual(fake.policies, {})
        self.assertTrue(all(c.get("revoked_at") for c in fake.chatbots.values()))
        self.assertEqual(fake.logic_installs, {})
        self.assertIn("skip", {c.action for c in removed})  # keys are revoked in the console

    def test_role_gate(self):
        fake = FakePlatform(role="analyst", installed_canons=["cn_seed_openai_agent_safety"])
        env = Env(fake, make_gov())
        with self.assertRaises(RequirementUnmet):
            apply(env.gov, env.ctx, plan(env.gov, env.ctx))

    def test_keys_are_minted_once_and_rotated_on_request(self):
        fake = FakePlatform(installed_canons=["cn_seed_openai_agent_safety"])
        env = Env(fake, make_gov())
        apply(env.gov, env.ctx, plan(env.gov, env.ctx))
        first = read_env_var(env.dir / "app.env", "DMZAGENT_APP_KEY")
        ctx2 = connect(make_gov(), env.path, platform=env.platform, env_file=env.dir / "app.env",
                       options={"rotate_keys": True})
        apply(make_gov(), ctx2, plan(make_gov(), ctx2))
        second = read_env_var(env.dir / "app.env", "DMZAGENT_APP_KEY")
        self.assertNotEqual(first, second)
        self.assertEqual(len(fake.minted_keys), 2)

    def test_verify_uses_the_engine_and_reports_requirements(self):
        fake = FakePlatform()
        env = Env(fake, make_gov())
        env.ctx.options["allow_missing_requirements"] = True
        apply(env.gov, env.ctx, plan(env.gov, env.ctx))
        results = verify(env.gov, env.ctx)
        by_name = {r["name"]: r for r in results}
        self.assertFalse(by_name["cn_seed_openai_agent_safety"]["ok"])
        self.assertTrue(by_name["scope creep holds"]["ok"], by_name["scope creep holds"]["detail"])

        wrong = make_gov()
        wrong.expectations.clear()
        wrong.expect("expects a block it will not get", labels={"rt_agent_scope_creep_v1": 0.9}, enforce="block")
        ctx2 = connect(wrong, env.path, platform=env.platform, env_file=env.dir / "app.env")
        self.assertFalse(verify(wrong, ctx2)[-1]["ok"])


class TestCli(unittest.TestCase):
    def test_load_governance_and_settings(self):
        d = Path(tempfile.mkdtemp())
        f = d / "governance.py"
        f.write_text("from giaas import Governance, settings\n"
                     "governance = Governance('cli-test')\n"
                     "governance.division_config(enforcement_posture=settings.get('posture', 'enforce'))\n")
        giaas.settings["posture"] = "observe"
        gov = load_governance(f)
        self.assertEqual(gov.by_kind("division_config")[0].settings["enforcement_posture"], "observe")
        giaas.settings.clear()

    def test_missing_key_is_a_clear_failure(self):
        d = Path(tempfile.mkdtemp())
        f = d / "governance.py"
        f.write_text("from giaas import Governance\ngovernance = Governance('cli-test')\n")
        saved = os.environ.pop("DMZAGENT_API_KEY", None)
        try:
            self.assertEqual(main(["plan", str(f), "--quiet"]), 2)
        finally:
            if saved:
                os.environ["DMZAGENT_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
