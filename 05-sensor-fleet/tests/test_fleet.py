"""The fleet controller against a fake logic door and breaker; the rulebook's
shape checked before it is ever published."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "giaas"))

import fleet  # noqa: E402
import governance as gov_module  # noqa: E402


class FakePlatform(BaseHTTPRequestHandler):
    events: list[dict] = []
    # A tiny accumulator: over-temp readings add 1; the breaker holds at 3.
    stress: dict[str, float] = {}
    # How many breaker checks to answer with 429 before behaving again.
    rate_limit_checks: int = 0

    def log_message(self, *a):
        pass

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/logic/soul"):
            return self._json(200, {"labels": [{"label": "chiller-thermal@1:over-temp", "tag": "thermal_stress", "strength": v}
                                               for v in self.stress.values()]})
        return self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.headers.get("authorization") != "Bearer ck_app":
            return self._json(401, {"detail": "not authenticated"})
        if self.path == "/v1/logic/events":
            ev = body["event"]; self.events.append(ev)
            subject = ev["subject_id"]
            escalations = []
            if ev.get("temp_c", 0) > 85:
                self.stress[subject] = self.stress.get(subject, 0) + 1
                band = "enforce" if self.stress[subject] >= 3 else "record"
                escalations.append({"rule_id": "over-temp", "band": band, "lane": band})
            fired = [{"rule_id": "sensor-offline"}] if ev.get("status") == "offline" else []
            return self._json(202, {"accepted": True, "fired": fired, "escalations": escalations})
        if self.path == "/v1/cb/check":
            if FakePlatform.rate_limit_checks > 0:
                FakePlatform.rate_limit_checks -= 1
                payload = json.dumps({"detail": "rate limited", "retry_after": 1}).encode()
                self.send_response(429); self.send_header("content-type", "application/json")
                self.send_header("retry-after", "1"); self.send_header("content-length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
                return
            subject = body["scope_ref"]
            held = self.stress.get(subject, 0) >= 3
            return self._json(200, {"state": "hold" if held else "closed", "allow": not held, "held": held,
                                    "warning": False, "reason": "enforce disposition on chiller-thermal@1:over-temp" if held else "default-allow",
                                    "fired_policies": [], "anchor": None, "checked_at": "", "latency_ms": 1, "route_latency_ms": 1})
        return self._json(404, {})


class TestRulebook(unittest.TestCase):
    def test_rulebook_shape(self):
        rb = gov_module.RULEBOOK
        ids = [r["id"] for r in rb["rules"]]
        self.assertEqual(ids, ["over-temp", "pressure-spike", "sensor-offline", "bad-reading"])
        for rule in rb["rules"]:
            self.assertIn("match", rule)
            self.assertTrue(rule.get("on_true"), rule["id"])
            for action in rule["on_true"]:
                self.assertIn("disposition", action)
        bands = rb["rules"][0]["accumulate"]["bands"]
        self.assertLess(bands["record"], bands["enforce"])
        self.assertLess(bands["enforce"], bands["human"])

    def test_governance_declares_logic_soul_policies(self):
        policies = gov_module.governance.by_kind("policy")
        self.assertTrue(all(p.soul == "logic" for p in policies))
        self.assertEqual(gov_module.governance.by_kind("logic_rulebook")[0].slug, "chiller-thermal")


class TestController(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakePlatform)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.fake.server_address[1]}"
        cls.ctl = fleet.Controller({"DMZAGENT_BASE_URL": base, "DMZAGENT_APP_KEY": "ck_app",
                                    "DMZAGENT_WORKSPACE_ID": "ws_logic", "DMZAGENT_DIVISION_ID": "dv_1"})

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()

    def setUp(self):
        # The fake keeps its accumulator and event log at class level so the
        # handler (re-created per request) can see them; start each test clean.
        FakePlatform.events.clear()
        FakePlatform.stress.clear()
        FakePlatform.rate_limit_checks = 0

    def test_subjects_are_sensor_typed_and_division_scoped(self):
        self.assertEqual(self.ctl.subject(fleet.Pump("pump-7")), "subject:dv_1:sensor:pump-7")

    def test_a_hot_pump_climbs_the_bands_and_is_held(self):
        pump = fleet.Pump("pump-7", hot_from=1, seed=1)
        rows = [self.ctl.tick(t, pump) for t in range(1, 5)]
        self.assertEqual(rows[0]["bands"], ["over-temp→record"])
        self.assertTrue(rows[0]["decision"]["allow"])
        self.assertEqual(rows[2]["bands"], ["over-temp→enforce"])
        self.assertFalse(rows[2]["decision"]["allow"])
        self.assertTrue(rows[2]["decision"]["held"])
        self.assertFalse(pump.running)
        self.assertEqual(FakePlatform.events[0]["subject_id"], "subject:dv_1:sensor:pump-7")
        self.assertIn("HOLD", fleet.render(rows[2]))

    def test_a_cool_pump_keeps_running(self):
        pump = fleet.Pump("pump-5", seed=2)
        row = self.ctl.tick(1, pump)
        self.assertEqual(row["bands"], [])
        self.assertTrue(pump.running)
        self.assertIn("RUN", fleet.render(row))

    def test_a_rate_limited_check_is_waited_out_once(self):
        FakePlatform.rate_limit_checks = 1
        pump = fleet.Pump("pump-3", seed=3)
        row = self.ctl.tick(1, pump)
        self.assertEqual(row["decision"]["state"], "closed")
        self.assertTrue(pump.running)

    def test_a_check_that_cannot_be_made_fails_closed(self):
        FakePlatform.rate_limit_checks = 5
        pump = fleet.Pump("pump-4", seed=4)
        row = self.ctl.tick(1, pump)
        self.assertEqual(row["decision"]["state"], "unknown")
        self.assertFalse(row["decision"]["allow"])
        self.assertFalse(pump.running)
        self.assertIn("STOP", fleet.render(row))

    def test_an_offline_sensor_fires_the_review_rule(self):
        pump = fleet.Pump("pump-9", offline_at=1)
        row = self.ctl.tick(1, pump)
        self.assertEqual(row["fired"], ["sensor-offline"])
        self.assertIn("offline", fleet.render(row))


if __name__ == "__main__":
    unittest.main()
