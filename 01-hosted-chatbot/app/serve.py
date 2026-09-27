"""The customer's site: a page that embeds the platform's chat widget, next
to a panel showing what the platform is deciding about that agent.

Standard library only. Reads app/.env (written by setup.sh). The browser
never sees an API key: the panel's reads and the hold/release drill go
through this server, which holds the analyst key setup.sh minted.

    python3 app/serve.py            # http://localhost:8000
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = json.loads(raw) if raw[0] == '"' else raw[1:-1]
        values[key.strip()] = raw
    return values


class Config:
    def __init__(self, env: dict[str, str]):
        self.base_url = env.get("DMZAGENT_BASE_URL", "https://api.dmzagent.com").rstrip("/")
        self.api_key = env.get("DMZAGENT_APP_KEY", "")
        self.workspace_id = env.get("DMZAGENT_WORKSPACE_ID", "")
        self.division_id = env.get("DMZAGENT_DIVISION_ID", "")
        self.embed_id = env.get("CHATBOT_EMBED_ID", "")
        self.agent_subject_id = env.get("CHATBOT_AGENT_SUBJECT_ID", "")
        self.embed_script_url = env.get("CHATBOT_EMBED_SCRIPT_URL", f"{self.base_url}/v1/embed/chat.js")
        self.embed_chat_url = env.get("CHATBOT_EMBED_CHAT_URL", f"{self.base_url}/v1/embed/{self.embed_id}/chat")
        self.agent_name = env.get("CHATBOT_AGENT_NAME", "Support")

    def missing(self) -> list[str]:
        need = {"DMZAGENT_APP_KEY": self.api_key, "DMZAGENT_WORKSPACE_ID": self.workspace_id,
                "CHATBOT_EMBED_ID": self.embed_id, "CHATBOT_AGENT_SUBJECT_ID": self.agent_subject_id}
        return [k for k, v in need.items() if not v]


class PlatformReader:
    """The few reads and two writes the panel needs, with the app key."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _call(self, method: str, path: str, body: dict | None = None, **query) -> tuple[int, dict]:
        url = self.cfg.base_url + path
        clean = {k: v for k, v in query.items() if v}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "authorization": f"Bearer {self.cfg.api_key}", "accept": "application/json",
            "content-type": "application/json", "user-agent": "dmzagent-examples/hosted-chatbot"})
        # The platform rate-limits per vendor (free tier: 10 requests a second).
        # A 429 says how long to wait; wait once rather than show a stale panel.
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read()
                    return resp.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = {"detail": raw.decode("utf-8", "replace")[:300]}
                if exc.code == 429 and attempt < 2:
                    time.sleep(float(exc.headers.get("Retry-After") or parsed.get("retry_after") or 1) + 0.1)
                    continue
                return exc.code, parsed
            except urllib.error.URLError as exc:
                return 0, {"detail": f"platform unreachable: {exc.reason}"}
        return 429, {"detail": "rate limited"}

    _policies_cache: tuple[float, list, list] | None = None

    def policies(self) -> tuple[list, list]:
        """Policies change rarely; read them at most every 30 seconds so the
        panel's polling stays well inside the rate limit."""
        now = time.monotonic()
        if self._policies_cache and now - self._policies_cache[0] < 30:
            return self._policies_cache[1], self._policies_cache[2]
        ws = self.cfg.workspace_id
        _, breakers = self._call("GET", "/v1/cb/policies", workspace_id=ws, limit=50)
        _, policies = self._call("GET", "/v1/policies", workspace_id=ws)
        pair = (breakers.get("policies", []) if isinstance(breakers, dict) else [],
                policies.get("policies", []) if isinstance(policies, dict) else [])
        self._policies_cache = (now, pair[0], pair[1])
        return pair

    def snapshot(self) -> dict:
        ws = self.cfg.workspace_id
        subject = self.cfg.agent_subject_id
        st, state = self._call("GET", f"/v1/cb/states/subject/{subject}", workspace_id=ws)
        _, decisions = self._call("GET", "/v1/cb/decisions", workspace_id=ws, scope_ref=subject, limit=10)
        _, reviews = self._call("GET", "/v1/reviews", workspace_id=ws, status="open")
        breaker_policies, policies = self.policies()
        return {
            "agent_subject_id": subject,
            "breaker": state if st == 200 else {"state": "unknown", "reason": state.get("detail")},
            "decisions": decisions.get("decisions", []) if isinstance(decisions, dict) else [],
            "reviews": reviews.get("reviews", []) if isinstance(reviews, dict) else [],
            "breaker_policies": breaker_policies,
            "policies": policies,
        }

    def override(self, action: str, reason: str) -> tuple[int, dict]:
        if action not in ("hold", "release"):
            return 400, {"detail": "action must be hold or release"}
        return self._call("POST", f"/v1/cb/{action}", {"workspace_id": self.cfg.workspace_id,
                                                        "subject_id": self.cfg.agent_subject_id,
                                                        "reason": reason or f"{action} from the demo panel"})

    def resolve_review(self, review_id: str, status: str, decision: str) -> tuple[int, dict]:
        if status not in ("resolved", "dismissed", "accepted"):
            return 400, {"detail": "status must be resolved, dismissed or accepted"}
        return self._call("POST", f"/v1/reviews/{urllib.parse.quote(review_id)}/resolve",
                          {"status": status, "decision": decision or status})


def render_page(cfg: Config) -> str:
    html = (HERE / "site" / "index.html").read_text()
    for key, value in {
        "EMBED_ID": cfg.embed_id, "DIVISION_ID": cfg.division_id, "WORKSPACE_ID": cfg.workspace_id,
        "EMBED_SCRIPT_URL": cfg.embed_script_url, "EMBED_CHAT_URL": cfg.embed_chat_url,
        "AGENT_NAME": cfg.agent_name, "AGENT_SUBJECT_ID": cfg.agent_subject_id,
    }.items():
        html = html.replace("{{" + key + "}}", _escape(value))
    return html


def _escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def make_handler(cfg: Config, reader: PlatformReader):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter than the default
            sys.stderr.write("%s %s\n" % (self.command, self.path))

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                return {}

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, render_page(cfg).encode(), "text/html; charset=utf-8")
            elif self.path == "/api/governance":
                self._json(200, reader.snapshot())
            elif self.path == "/healthz":
                self._json(200, {"ok": True, "missing": cfg.missing()})
            else:
                self._json(404, {"detail": "not found"})

        def do_POST(self):
            body = self._body()
            if self.path in ("/api/drill/hold", "/api/drill/release"):
                status, data = reader.override(self.path.rsplit("/", 1)[1], body.get("reason", ""))
                self._json(status or 502, data)
            elif self.path.startswith("/api/reviews/") and self.path.endswith("/resolve"):
                review_id = self.path[len("/api/reviews/"):-len("/resolve")]
                status, data = reader.resolve_review(urllib.parse.unquote(review_id),
                                                     body.get("status", "resolved"), body.get("decision", ""))
                self._json(status or 502, data)
            else:
                self._json(404, {"detail": "not found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    port = int(os.environ.get("PORT") or (argv[0] if argv else 8000))
    cfg = Config(load_env(HERE / ".env"))
    missing = cfg.missing()
    if missing:
        print(f"app/.env is missing {', '.join(missing)} — run ./setup.sh first", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, PlatformReader(cfg)))
    print(f"Harbor Supply site: http://localhost:{port}  (agent {cfg.agent_subject_id})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
