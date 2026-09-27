"""The operations desk: every governed subject, the review queue, the
ledger-anchored decisions, and the controls a person uses.

Standard library only. Reads app/.env (written by setup.sh). The browser
never sees the key; the desk holds the analyst key setup.sh minted and does
the platform calls itself.

    python3 app/desk.py            # http://localhost:8002

It also listens on POST /hooks/remediate for the remediation directives
the manifest points at this machine, and shows them.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                values[k.strip()] = json.loads(v) if v.startswith('"') else v
    return values


class Desk:
    def __init__(self, env: dict[str, str]):
        self.base_url = env.get("DMZAGENT_BASE_URL", "https://api.dmzagent.com").rstrip("/")
        self.api_key = env.get("DMZAGENT_APP_KEY", "")
        self.workspace_id = env.get("DMZAGENT_WORKSPACE_ID", "")
        self.division_id = env.get("DMZAGENT_DIVISION_ID", "")
        self.deliveries: list[dict] = []
        self.lock = threading.Lock()
        self._config_cache: tuple[float, dict] | None = None

    def missing(self) -> list[str]:
        need = {"DMZAGENT_APP_KEY": self.api_key, "DMZAGENT_WORKSPACE_ID": self.workspace_id,
                "DMZAGENT_DIVISION_ID": self.division_id}
        return [k for k, v in need.items() if not v]

    # --- platform calls ----------------------------------------------------- #

    def call(self, method: str, path: str, body: dict | None = None, **query) -> tuple[int, dict]:
        url = self.base_url + path
        clean = {k: v for k, v in query.items() if v}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "authorization": f"Bearer {self.api_key}", "accept": "application/json",
            "content-type": "application/json", "user-agent": "dmzagent-examples/review-desk"})
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

    def posture(self) -> dict:
        now = time.monotonic()
        if self._config_cache and now - self._config_cache[0] < 30:
            return self._config_cache[1]
        st, data = self.call("GET", f"/v1/divisions/{self.division_id}/config")
        cfg = (data.get("config") or {}) if st == 200 else {}
        self._config_cache = (now, cfg)
        return cfg

    def snapshot(self) -> dict:
        ws = self.workspace_id
        _, states = self.call("GET", "/v1/cb/states", workspace_id=ws, limit=50)
        _, reviews = self.call("GET", "/v1/reviews", workspace_id=ws, status="open")
        _, decisions = self.call("GET", "/v1/cb/decisions", workspace_id=ws, limit=15)
        cfg = self.posture()
        with self.lock:
            deliveries = list(self.deliveries)[-10:]
        return {
            "workspace_id": ws, "division_id": self.division_id,
            "posture": cfg.get("enforcement_posture", "enforce"),
            "reasoning_mode": cfg.get("reasoning_mode", "per_trace"),
            "subjects": states.get("states", []) if isinstance(states, dict) else [],
            "reviews": reviews.get("reviews", []) if isinstance(reviews, dict) else [],
            "decisions": decisions.get("decisions", []) if isinstance(decisions, dict) else [],
            "deliveries": deliveries,
        }

    # --- operator actions ----------------------------------------------------- #

    def subject_override(self, action: str, subject_id: str, reason: str) -> tuple[int, dict]:
        if action not in ("hold", "release", "engage"):
            return 400, {"detail": "action must be hold, release or engage"}
        return self.call("POST", f"/v1/cb/{action}", {"workspace_id": self.workspace_id,
                                                      "subject_id": subject_id,
                                                      "reason": reason or f"{action} from the desk"})

    def review_action(self, review_id: str, action: str, body: dict) -> tuple[int, dict]:
        rid = urllib.parse.quote(review_id, safe="")
        if action == "claim":
            return self.call("POST", f"/v1/reviews/{rid}/claim", {"release": bool(body.get("unclaim"))})
        if action == "resolve":
            status = body.get("status", "resolved")
            if status not in ("resolved", "dismissed", "accepted"):
                return 400, {"detail": "status must be resolved, dismissed or accepted"}
            return self.call("POST", f"/v1/reviews/{rid}/resolve",
                             {"status": status, "decision": body.get("decision") or status})
        if action in ("release", "hold", "escalate"):
            payload = {"reason": body.get("reason") or f"{action} from the desk"}
            if action == "escalate":
                payload["to_tier"] = body.get("to_tier", "division")
            return self.call("POST", f"/v1/reviews/{rid}/{action}", payload)
        return 400, {"detail": "unknown review action"}

    def receive_delivery(self, headers: dict, body: dict) -> None:
        with self.lock:
            self.deliveries.append({"at": time.strftime("%H:%M:%S"),
                                    "event": headers.get("x-dmzagent-event") or headers.get("X-DMZAgent-Event"),
                                    "directive": body})
            del self.deliveries[:-50]


def make_handler(desk: Desk):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.command, self.path))

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                html = (HERE / "site" / "index.html").read_text()
                self._send(200, html.replace("{{WORKSPACE_ID}}", desk.workspace_id).encode(), "text/html; charset=utf-8")
            elif self.path == "/api/desk":
                self._json(200, desk.snapshot())
            elif self.path == "/healthz":
                self._json(200, {"ok": True, "missing": desk.missing()})
            else:
                self._json(404, {"detail": "not found"})

        def do_POST(self):
            body = self._body()
            parts = [p for p in self.path.split("/") if p]
            if parts[:2] == ["api", "subjects"] and len(parts) == 3:
                status, data = desk.subject_override(parts[2], body.get("subject_id", ""), body.get("reason", ""))
                self._json(status or 502, data)
            elif parts[:2] == ["api", "reviews"] and len(parts) == 4:
                status, data = desk.review_action(urllib.parse.unquote(parts[2]), parts[3], body)
                self._json(status or 502, data)
            elif self.path == "/hooks/remediate":
                desk.receive_delivery(dict(self.headers.items()), body)
                self._json(202, {"received": True})
            else:
                self._json(404, {"detail": "not found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    desk = Desk(load_env(HERE / ".env"))
    missing = desk.missing()
    if missing:
        print(f"app/.env is missing {', '.join(missing)} — run ./setup.sh first", file=sys.stderr)
        return 2
    port = int(os.environ.get("PORT") or (argv[0] if argv else 8002))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(desk))
    print(f"Harbor Supply operations desk: http://localhost:{port}  (workspace {desk.workspace_id})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
