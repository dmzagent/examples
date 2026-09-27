"""An in-memory stand-in for the operator surface the toolkit drives.

It implements just the routes `giaas` calls, with the response shapes the
real server returns (checked against a live instance), so the engine's
tests run with no network and no credentials.
"""
from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, urlsplit


class FakePlatform:
    def __init__(self, *, role: str = "tenant_admin", installed_canons: list[str] | None = None,
                 corpus_install_allowed: bool = False, rate_limit_first: int = 0):
        self.workspace_id = "ws_test"
        self.division_id = "dv_test"
        self.role = role
        self.config: dict = {}
        self.canons = [{"canon_id": c, "name": c, "latest_version": "1.0.0", "enabled": True}
                       for c in (installed_canons or ["cn_core_concordex"])]
        self.corpus_install_allowed = corpus_install_allowed
        self.cb_policies: dict[str, dict] = {}
        self.policies: dict[str, dict] = {}
        self.chatbots: dict[str, dict] = {}
        self.logic_canons: dict[str, dict] = {}
        self.logic_versions: dict[tuple[str, int], dict] = {}
        self.logic_installs: dict[str, int] = {}
        self.minted_keys: list[dict] = []
        self.sessions: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.rate_limit_first = rate_limit_first

    # The transport signature the client expects.
    def __call__(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float):
        parts = urlsplit(url)
        path, query = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        payload = json.loads(body) if body else {}
        self.calls.append((method, path))
        if self.rate_limit_first > 0:
            self.rate_limit_first -= 1
            return 429, {"Retry-After": "0.01"}, json.dumps({"error": "rate_limited", "retry_after": 1}).encode()
        if not headers.get("authorization", "").startswith("Bearer ck_"):
            return 401, {}, b'{"detail": "not authenticated"}'
        status, data = self.route(method, path, query, payload)
        return status, {}, json.dumps(data).encode()

    def route(self, method: str, path: str, query: dict, payload: dict):
        if path == "/v1/auth/me":
            return 200, {"authenticated": True, "user": {"user_id": "apikey:ak_1", "vendor_id": "vd_1",
                                                          "workspace_roles": {self.workspace_id: self.role},
                                                          "kind": "tenant"}}
        if path == "/v1/agent-sessions" and method == "POST":
            sid = f"sess_{uuid.uuid4().hex[:8]}"
            self.sessions.append(sid)
            return 201, {"session_id": sid, "workspace_id": self.workspace_id, "division_id": self.division_id}
        if path.startswith("/v1/agent-sessions/") and method == "DELETE":
            self.sessions.remove(path.rsplit("/", 1)[1])
            return 200, {"status": "closed"}
        if path == f"/v1/divisions/{self.division_id}/config":
            if method == "PUT":
                self.config = dict(payload.get("config") or {})
            return 200, {"config": dict(self.config)}
        if path == "/mcp/v1":
            if payload.get("method") == "resources/read":
                text = json.dumps({"count": len(self.canons), "canons": self.canons})
                return 200, {"jsonrpc": "2.0", "id": 1, "result": {"contents": [{"uri": "concordia:/workspace/canons", "text": text}]}}
            return 200, {"jsonrpc": "2.0", "id": 1, "result": {}}
        if path == "/v1/corpus/install":
            if not self.corpus_install_allowed:
                return 403, {"detail": "This endpoint is dashboard-only; API keys are for the SDK surface."}
            self.canons.append({"canon_id": payload["canon_id"], "name": payload["canon_id"],
                                "latest_version": payload.get("version") or "1.0.0", "enabled": True})
            return 200, {"installed_version": payload.get("version") or "1.0.0"}
        if path == "/v1/cb/policies":
            if method == "GET":
                return 200, {"policies": list(self.cb_policies.values()), "next_cursor": None}
            pid = payload.get("cb_policy_id") or f"cbp_{uuid.uuid4().hex[:12]}"
            row = {"cb_policy_id": pid, "workspace_id": payload["workspace_id"], "name": payload["name"],
                   "rules": payload.get("rules") or [], "action": payload["action"],
                   "scope": payload.get("scope") or "subject", "enabled": bool(payload.get("enabled", True)),
                   "description": payload.get("description")}
            self.cb_policies[pid] = row
            return 200, row
        if path.startswith("/v1/cb/policies/") and method == "DELETE":
            pid = path.rsplit("/", 1)[1]
            self.cb_policies.pop(pid, None)
            return 200, {"deleted": pid}
        if path == "/v1/policies":
            if method == "GET":
                return 200, {"workspace_id": self.workspace_id, "policies": list(self.policies.values())}
            pid = payload.get("policy_id") or f"pol_{uuid.uuid4().hex[:14]}"
            row = {"policy_id": pid, "workspace_id": payload["workspace_id"], "name": payload["name"],
                   "conditions": payload.get("conditions") or [], "lane": payload["lane"],
                   "level": payload.get("level"), "soul": payload.get("soul") or "reasoning",
                   "config": payload.get("config") or {}, "status": "active",
                   "enabled": bool(payload.get("enabled", True)), "description": payload.get("description")}
            self.policies[pid] = row
            return 201, row
        if path.startswith("/v1/policies/") and method == "DELETE":
            self.policies.pop(path.rsplit("/", 1)[1], None)
            return 204, {}
        if path == "/v1/policies/evaluate":
            labels = payload.get("labels") or {}
            fired = set(payload.get("fired") or [])
            decisions = []
            for p in self.policies.values():
                if p.get("soul") not in (payload.get("soul"), "any"):
                    continue
                ok = True
                for c in p["conditions"]:
                    if c["kind"] == "presence":
                        ok = ok and (c["label"] in labels or c["label"] in fired)
                    else:
                        v = labels.get(c["label"])
                        ok = ok and v is not None and _cmp(c.get("op", ">="), v, c["threshold"])
                if ok:
                    decisions.append({"policy_id": p["policy_id"], "name": p["name"], "lane": p["lane"], "level": p["level"]})
            rank = {"allow": 0, "challenge": 1, "hold": 2, "block": 3, "notify": 0, "review": 1, "escalate": 2}
            resolved = {}
            for lane in ("enforce", "coordinate"):
                cands = [d for d in decisions if d["lane"] == lane]
                if cands:
                    resolved[lane] = max(cands, key=lambda d: rank.get(d["level"], 0))
            rem = [d for d in decisions if d["lane"] == "remediate"]
            if rem:
                resolved["remediate"] = rem
            return 200, {"decisions": decisions, "resolved": resolved}
        if path == "/v1/chatbot-definitions":
            if method == "GET":
                return 200, [c for c in self.chatbots.values() if not c.get("revoked_at")]
            eid = f"cb_{uuid.uuid4().hex[:16]}"
            row = {"embed_id": eid, "division_id": self.division_id, "workspace_id": payload["workspace_id"],
                   "agent_name": payload["agent_name"], "site_domain": payload.get("site_domain") or "",
                   "protected_action": payload.get("protected_action"), "config": payload.get("config") or {},
                   "connector_instance_id": "ci_1", "revoked_at": None}
            self.chatbots[eid] = row
            return 201, row
        if path.startswith("/v1/chatbot-definitions/"):
            eid = path.split("/")[3]
            row = self.chatbots.get(eid)
            if not row:
                return 404, {"detail": "chatbot definition not found"}
            if method == "PATCH":
                for k in ("site_domain", "protected_action", "config"):
                    if k in payload:
                        row[k] = payload[k]
                return 200, row
            if method == "DELETE":
                row["revoked_at"] = "now"
                return 204, {}
        if path == "/v1/logic-canons":
            if method == "GET":
                return 200, {"logic_canons": list(self.logic_canons.values())}
            cid = f"lcanon_{uuid.uuid4().hex[:12]}"
            row = {"logic_canon_id": cid, "slug": payload["slug"], "name": payload["name"],
                   "status": "draft", "latest_version": None}
            self.logic_canons[cid] = row
            return 200, row
        if path.startswith("/v1/logic-canons/"):
            bits = path.split("/")
            cid = bits[3]
            canon = self.logic_canons.get(cid)
            if not canon:
                return 404, {"detail": "logic canon not found"}
            if len(bits) == 5 and bits[4] == "versions" and method == "POST":
                v = (canon["latest_version"] or 0) + 1
                doc = dict(payload["rulebook"]); doc.update({"logic_canon": canon["slug"], "version": v, "visibility": "private"})
                self.logic_versions[(cid, v)] = {"logic_canon_id": cid, "version": v, "rulebook": doc}
                canon["latest_version"] = v; canon["status"] = "published"
                return 200, self.logic_versions[(cid, v)]
            if len(bits) == 6 and bits[4] == "versions" and method == "GET":
                return 200, self.logic_versions[(cid, int(bits[5]))]
            if len(bits) == 5 and bits[4] == "install" and method == "POST":
                self.logic_installs[cid] = payload.get("version") or canon["latest_version"]
                return 200, {"logic_canon_id": cid, "version": self.logic_installs[cid], "bridged_policies": 2}
            if len(bits) == 6 and bits[4] == "install" and method == "DELETE":
                self.logic_installs.pop(cid, None)
                return 200, {}
            if len(bits) == 5 and bits[4] == "unpublish":
                canon["status"] = "unpublished"
                return 200, canon
        if path == f"/v1/workspaces/{self.workspace_id}/logic-canons":
            return 200, {"installs": [{"logic_canon_id": c, "version": v} for c, v in self.logic_installs.items()]}
        if path == "/v1/agent-stream/api-keys":
            key = f"ck_{uuid.uuid4().hex}"
            self.minted_keys.append({"label": payload.get("label"), "key": key})
            return 200, {"key": key, "workspace_id": self.workspace_id, "scopes": "agent_stream:write,cb:check"}
        return 404, {"detail": f"fake platform: no route for {method} {path}"}


def _cmp(op: str, a: float, b: float) -> bool:
    return {">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b, "==": a == b, "!=": a != b}[op]
