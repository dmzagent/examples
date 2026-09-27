"""The site server, against a fake platform on localhost."""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import serve  # noqa: E402


class FakePlatformHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, dict]] = []

    def log_message(self, *a):  # silence
        pass

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.seen.append(("GET", self.path, {}))
        p = self.path
        if p.startswith("/v1/cb/states/subject/"):
            return self._json(200, {"state": "half_open", "reason": "Review on scope creep", "exists": True})
        if p.startswith("/v1/cb/decisions"):
            return self._json(200, {"decisions": [{"state_before": "closed", "state_after": "half_open",
                                                   "fired_policies": [{"name": "Review on scope creep"}],
                                                   "ledger_event_id": "abc12345-x", "created_at": "2026-09-27T10:00:00Z"}]})
        if p.startswith("/v1/reviews"):
            return self._json(200, {"reviews": [{"review_id": "rev_1", "tag_id": "rt_agent_scope_creep_v1",
                                                 "subject_id": "subject:dv:chat-agent:cb_1", "level": "review"}]})
        if p.startswith("/v1/cb/policies"):
            return self._json(200, {"policies": [{"name": "Block on PII leak", "action": "block",
                                                  "rules": [{"tag": "rt_agent_pii_leak_v1", "op": ">=", "value": 0.5}]}]})
        if p.startswith("/v1/policies"):
            return self._json(200, {"policies": [{"name": "Hold", "lane": "enforce", "level": "hold", "conditions": []}]})
        return self._json(404, {"detail": "no route"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self.seen.append(("POST", self.path, body))
        if self.path in ("/v1/cb/hold", "/v1/cb/release"):
            if self.headers.get("authorization") != "Bearer ck_app":
                return self._json(401, {"detail": "not authenticated"})
            return self._json(200, {"state": {"state": "hold" if self.path.endswith("hold") else "closed"}})
        if self.path.endswith("/resolve"):
            return self._json(200, {"review_id": "rev_1", "status": body.get("status")})
        return self._json(404, {"detail": "no route"})


class TestSiteServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakePlatformHandler)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.fake.server_address[1]}"
        cls.cfg = serve.Config({
            "DMZAGENT_BASE_URL": base, "DMZAGENT_APP_KEY": "ck_app", "DMZAGENT_WORKSPACE_ID": "ws_1",
            "DMZAGENT_DIVISION_ID": "dv_1", "CHATBOT_EMBED_ID": "cb_1",
            "CHATBOT_AGENT_SUBJECT_ID": "subject:dv_1:chat-agent:cb_1", "CHATBOT_AGENT_NAME": "Harbor helper",
        })
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(cls.cfg, serve.PlatformReader(cls.cfg)))
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.site.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown(); cls.fake.shutdown()

    def _get(self, path):
        with urllib.request.urlopen(self.url + path) as r:
            return r.status, r.read().decode()

    def _post(self, path, body):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def test_page_embeds_the_platform_widget_for_this_agent(self):
        status, html = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn('data-agent-id="cb_1"', html)
        self.assertIn('data-endpoint="' + self.cfg.embed_chat_url + '"', html)
        self.assertIn('id="dmzagent-chat-cb_1"', html)
        self.assertNotIn("ck_app", html)  # the key never reaches the browser

    def test_panel_reads_breaker_reviews_decisions_and_policies(self):
        status, body = self._get("/api/governance")
        snap = json.loads(body)
        self.assertEqual(snap["breaker"]["state"], "half_open")
        self.assertEqual(snap["reviews"][0]["review_id"], "rev_1")
        self.assertEqual(snap["decisions"][0]["state_after"], "half_open")
        self.assertEqual(snap["breaker_policies"][0]["name"], "Block on PII leak")
        self.assertEqual(snap["policies"][0]["lane"], "enforce")

    def test_drill_holds_and_releases_the_agent_subject(self):
        status, data = self._post("/api/drill/hold", {"reason": "drill"})
        self.assertEqual(status, 200)
        call = [c for c in FakePlatformHandler.seen if c[1] == "/v1/cb/hold"][-1]
        self.assertEqual(call[2], {"workspace_id": "ws_1", "subject_id": "subject:dv_1:chat-agent:cb_1", "reason": "drill"})
        status, data = self._post("/api/drill/release", {})
        self.assertEqual(data["state"]["state"], "closed")

    def test_reviews_can_be_resolved_from_the_panel(self):
        status, data = self._post("/api/reviews/rev_1/resolve", {"status": "dismissed"})
        self.assertEqual(data["status"], "dismissed")
        call = [c for c in FakePlatformHandler.seen if c[1].endswith("/resolve")][-1]
        self.assertEqual(call[2]["status"], "dismissed")

    def test_missing_configuration_is_named(self):
        self.assertEqual(sorted(serve.Config({}).missing()),
                         ["DMZAGENT_APP_KEY", "DMZAGENT_DIVISION_ID", "DMZAGENT_WORKSPACE_ID", "DMZ_CHATBOT_SUPPORT_BOT"])
        cfg = serve.Config({"DMZAGENT_DIVISION_ID": "dv_1", "DMZ_CHATBOT_SUPPORT_BOT": "emb_42"})
        self.assertEqual(cfg.agent_subject_id, "subject:dv_1:chat-agent:emb_42")


if __name__ == "__main__":
    unittest.main()
