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
        self.valid_keys: set[str] | None = None      # None: any ck_ key authenticates
        # Operations state: breakers per subject, their transitions, reviews, the ledger, souls.
        self.breakers: dict[str, dict] = {}
        self.decisions: list[dict] = []
        self.reviews: dict[str, dict] = {}
        self.ledger: list[dict] = []
        self.souls: dict[str, dict] = {}
        self.mcp_calls: list[tuple[str, dict]] = []
        # The Solution Manifest surface: stacks by name, each with its managed
        # resources and versions; drift items a test can plant.
        self.stacks: dict[str, dict] = {}
        self.drift: list[dict] = []
        self.last_manifest: dict | None = None

    # The transport signature the client expects.
    def __call__(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float):
        parts = urlsplit(url)
        path, query = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        payload = json.loads(body) if body else {}
        self.calls.append((method, path))
        if self.rate_limit_first > 0:
            self.rate_limit_first -= 1
            return 429, {"Retry-After": "0.01"}, json.dumps({"error": "rate_limited", "retry_after": 1}).encode()
        bearer = headers.get("authorization", "")
        if not bearer.startswith("Bearer ck_") or (self.valid_keys is not None and bearer[7:] not in self.valid_keys):
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
            return 200, self.mcp(payload)
        if path == "/v1/cb/check" and method == "POST":
            return 200, self.check(payload.get("scope_ref") or "")
        if path in ("/v1/cb/hold", "/v1/cb/release", "/v1/cb/engage") and method == "POST":
            action = path.rsplit("/", 1)[1]
            target = {"hold": "hold", "release": "closed", "engage": "open"}[action]
            row = self.transition(payload.get("subject_id") or "", target,
                                  f"manual override by apikey:ak_1: {payload.get('reason', '')}", manual=True)
            return 200, {"state": row}
        if path == "/v1/cb/states":
            return 200, {"workspace_id": self.workspace_id, "states": list(self.breakers.values())[: int(query.get("limit", 50))]}
        if path == "/v1/cb/decisions":
            return 200, {"workspace_id": self.workspace_id,
                         "decisions": list(reversed(self.decisions))[: int(query.get("limit", 15))]}
        if path == "/v1/reviews" and method == "GET":
            status = query.get("status")
            rows = [r for r in self.reviews.values() if not status or r["status"] == status]
            return 200, {"workspace_id": self.workspace_id, "status": status or "all", "reviews": rows}
        if path.startswith("/v1/reviews/") and method == "POST":
            _, _, _, rid, action = path.split("/", 4)
            review = self.reviews.get(rid)
            if not review:
                return 404, {"detail": "review not found"}
            if action == "claim":
                review["claimed_by"] = None if payload.get("release") else "apikey:ak_1"
                review["status"] = "open" if payload.get("release") else "claimed"
            elif action == "resolve":
                review["status"] = payload.get("status", "resolved")
                review["decision"] = payload.get("decision")
            elif action in ("release", "hold", "escalate"):
                review["status"] = {"release": "resolved", "hold": "open", "escalate": "escalated"}[action]
                review["last_reason"] = payload.get("reason")
                if action == "release":
                    self.transition(review["subject_id"], "closed", f"released from review {rid}", manual=True)
                if action == "hold":
                    self.transition(review["subject_id"], "hold", f"held from review {rid}", manual=True)
            else:
                return 404, {"detail": "unknown action"}
            return 200, {"review": review}
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
            if self.role != "tenant_admin":
                return 403, {"detail": "tenant_admin required"}
            key = f"ck_{uuid.uuid4().hex}"
            self.minted_keys.append({"label": payload.get("label"), "key": key, "workspace_id": payload.get("workspace_id")})
            return 200, {"key": key, "workspace_id": payload.get("workspace_id"), "scopes": "agent_stream:write,cb:check"}
        if path == "/v1/manifests/validate":
            return self.mf_validate(payload)
        if path == "/v1/manifests/plan":
            return self.mf_plan(payload)
        if path == "/v1/manifests/apply":
            return self.mf_apply(payload)
        if path == "/v1/stacks" and method == "GET":
            return 200, {"vendor_id": "vd_1", "stacks": [self._stack_row(s) for s in self.stacks.values()]}
        if path.startswith("/v1/stacks/"):
            parts = path.split("/")
            stack = next((s for s in self.stacks.values() if s["stack_id"] == parts[3]), None)
            if not stack:
                return 404, {"detail": "stack not found"}
            if len(parts) == 4 and method == "GET":
                return 200, {**self._stack_row(stack), "versions": stack["versions"],
                             "resources": [{"kind": r["kind"], "logical_id": lid, "physical_id": r["physical_id"],
                                            "deletion_policy": "retain"} for lid, r in stack["resources"].items()],
                             "outputs": []}
            if len(parts) == 5 and parts[4] == "destroy":
                return self.mf_destroy(stack, payload)
            if len(parts) == 5 and parts[4] == "drift":
                return self.mf_drift(stack, payload)
        return 404, {"detail": f"fake platform: no route for {method} {path}"}

    # ----------------------------------------------------------------- #
    # Operations: breakers, decisions, reviews, the ledger
    # ----------------------------------------------------------------- #

    def transition(self, subject: str, state: str, reason: str, *, manual: bool = False) -> dict:
        before = (self.breakers.get(subject) or {}).get("state", "closed")
        anchor = self.append_ledger({"kind": "cb.transition", "subject_id": subject, "state": state})
        row = {"cb_state_id": f"cbs_{len(self.decisions) + 1}", "division_id": self.division_id, "scope": "subject",
               "scope_ref": subject, "state": state, "reason": reason,
               "last_decision": {"fired_policies": [{"by": "apikey:ak_1", "manual": manual, "reason": reason}], "reason": reason},
               "ledger_event_id": anchor, "transitioned_at": "2026-09-27T12:00:00.000Z", "updated_at": "2026-09-27T12:00:00.000Z"}
        self.breakers[subject] = row
        self.decisions.append({"decision_id": f"cbd_{len(self.decisions) + 1}", "division_id": self.division_id,
                               "scope": "subject", "scope_ref": subject, "state_before": before, "state_after": state,
                               "fired_policies": [{"manual": manual, "by": "apikey:ak_1", "reason": reason}],
                               "ledger_event_id": anchor, "created_at": "2026-09-27T12:00:00.000Z"})
        return row

    def check(self, subject: str) -> dict:
        row = self.breakers.get(subject)
        state = row["state"] if row else "closed"
        allow = state in ("closed", "half_open")
        return {"state": state, "allow": allow, "warning": state == "half_open", "held": state == "hold",
                "reason": row["reason"] if row else "no cached state (default-allow)",
                "fired_policies": row["last_decision"]["fired_policies"] if row else [],
                "anchor": row["ledger_event_id"] if row else None, "checked_at": "2026-09-27T12:00:00.000Z",
                "latency_ms": 1, "route_latency_ms": 1}

    def open_review(self, subject: str, tag: str = "rt_agent_tool_misuse_v1", level: str = "review") -> dict:
        rid = f"rv_{len(self.reviews) + 1}"
        self.reviews[rid] = {"review_id": rid, "workspace_id": self.workspace_id, "division_id": self.division_id,
                             "subject_id": subject, "tag_id": tag, "level": level, "status": "open", "tier": "workspace",
                             "claimed_by": None, "created_at": "2026-09-27T12:00:00.000Z"}
        return self.reviews[rid]

    def append_ledger(self, payload: dict) -> str:
        event_id = str(uuid.uuid4())
        self.ledger.append({"event_id": event_id, "index": len(self.ledger), "payload": payload,
                            "hash": uuid.uuid4().hex * 2})
        return event_id

    # ----------------------------------------------------------------- #
    # The MCP server: the wire shapes of /mcp/v1
    # ----------------------------------------------------------------- #

    TOOLS = [
        {"name": "enforce_covenant", "description": "Pre-flight guard. Writes to the ledger; analyst+.",
         "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "string"}, "action_kind": {"type": "string"},
                                                          "action_payload": {"type": "object"}, "context": {"type": "object"}},
                         "required": ["subject_id", "action_kind"], "additionalProperties": False}},
        {"name": "record_decision", "description": "Post-hoc audit. Writes to the ledger; analyst+.",
         "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "string"}, "decision_kind": {"type": "string"},
                                                          "payload": {"type": "object"}, "actor": {"type": "string"}, "outcome": {"type": "string"}},
                         "required": ["subject_id", "decision_kind"]}},
        {"name": "query_corpus", "description": "Read. Search the installed Canons. Viewer+.",
         "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
        {"name": "get_subject_soul", "description": "Read. A subject's soul snapshot. Viewer+.",
         "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "string"}}, "required": ["subject_id"]}},
        {"name": "get_division_config", "description": "Read a division's config.",
         "inputSchema": {"type": "object", "properties": {"division_id": {"type": "string"}}, "required": ["division_id"]}},
    ]
    RESOURCES = [
        {"uri": "concordia:/workspace/policies", "name": "Workspace circuit-breaker policies", "description": "Every CB policy.", "mimeType": "application/json"},
        {"uri": "concordia:/workspace/canons", "name": "Installed Canons", "description": "Every installed Canon.", "mimeType": "application/json"},
        {"uri": "concordia:/workspace/recent-ledger", "name": "Recent ledger entries", "description": "Paginated ledger.", "mimeType": "application/json"},
    ]

    def canonical(self, subject: str) -> str:
        return subject if subject.startswith("subject:") else f"subject:{self.division_id}:{subject}"

    def mcp(self, body: dict) -> dict:
        jid, method, params = body.get("id"), body.get("method"), body.get("params") or {}

        def ok(result):
            return {"jsonrpc": "2.0", "id": jid, "result": result}

        def err(code, message, error_id=None):
            data = {"error_id": error_id} if error_id else None
            e = {"code": code, "message": message}
            if data:
                e["data"] = data
            return {"jsonrpc": "2.0", "id": jid, "error": e}

        if method == "initialize":
            return ok({"protocolVersion": "1.0", "serverInfo": {"name": "concordia", "version": "1.0.0", "build": "fake"},
                       "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False, "subscribe": False}},
                       "principalHint": {"workspace_id": self.workspace_id, "role": self.role}})
        if method == "ping":
            return ok({"ok": True})
        if method == "tools/list":
            return ok({"tools": self.TOOLS})
        if method == "resources/list":
            return ok({"resources": self.RESOURCES})
        if method == "resources/read":
            uri = params.get("uri") or ""
            base, _, query = uri.partition("?")
            q = {k: v[0] for k, v in parse_qs(query).items()}
            if base == "concordia:/workspace/canons":
                doc = {"count": len(self.canons), "canons": self.canons}
            elif base == "concordia:/workspace/policies":
                doc = {"count": len(self.cb_policies), "policies": list(self.cb_policies.values())}
            elif base == "concordia:/workspace/recent-ledger":
                since, limit = int(q.get("since", 0)), int(q.get("limit", 100))
                rows = self.ledger[since: since + limit]
                doc = {"since": since, "limit": limit, "count": len(rows), "entries": rows,
                       "next_since": (since + len(rows)) if since + len(rows) < len(self.ledger) else None,
                       "workspace_id": self.workspace_id}
            else:
                return err(-32602, f"Unknown resource: {uri}")
            return ok({"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(doc, sort_keys=True)}]})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            self.mcp_calls.append((name, args))
            if name not in {t["name"] for t in self.TOOLS}:
                return err(-32601, f"Unknown tool: {name}")
            if name in ("enforce_covenant", "record_decision") and self.role not in ("analyst", "tenant_admin"):
                return err(-32007, f"Role '{self.role}' cannot call tool '{name}'", "permission_denied")
            if name == "enforce_covenant":
                subject = self.canonical(args.get("subject_id") or "")
                if not args.get("action_kind"):
                    return err(-32602, "action_kind required")
                chk = self.check(subject)
                verdict = "allow" if chk["allow"] and not chk["warning"] else ("review" if chk["allow"] else "block")
                anchor = self.append_ledger({"kind": "mcp.enforce_covenant", "subject_id": subject,
                                             "action_kind": args["action_kind"], "verdict": verdict})
                data = {"verdict": verdict, "policy_ids": [], "rationale": chk["reason"],
                        "cb_state_change": {"state": chk["state"], "warning": chk["warning"], "soul_version": None},
                        "ledger_entry_id": anchor}
            elif name == "record_decision":
                anchor = self.append_ledger({"kind": "mcp.record_decision", **args})
                data = {"ledger_entry_id": anchor, "chain_head_hash": self.ledger[-1]["hash"], "index": len(self.ledger) - 1}
            elif name == "query_corpus":
                q = (args.get("query") or "").lower()
                matches = [{"canon_id": c["canon_id"], "name": c["name"]} for c in self.canons if q in c["name"].lower()]
                data = {"matches": matches[: int(args.get("limit") or 20)], "scope": "installed", "canon_count": len(self.canons)}
            elif name == "get_subject_soul":
                subject = self.canonical(args.get("subject_id") or "")
                soul = self.souls.get(subject)
                if not soul:
                    return err(-32005, f"No soul snapshot for subject {args.get('subject_id')} in this workspace", "subject_not_found")
                data = {"subject_id": subject, "tags": soul, "soul_version": 1, "retired_count": 0, "recent_traces": []}
            else:
                # The live server's extension tools crash today; mirror that so callers cope.
                return err(-32603, "Tool execution failed", None) | {"error": {"code": -32603, "message": "Tool execution failed", "data": {"error_id": "deadbeef0000"}}}
            return ok({"content": [{"type": "text", "text": json.dumps(data, sort_keys=True)}], "_data": data,
                       "isError": False, "_latency_ms": 1.0})
        return err(-32601, f"Unknown method: {method}")

    # ----------------------------------------------------------------- #
    # The Solution Manifest surface (JSON manifests; the real server parses YAML)
    # ----------------------------------------------------------------- #

    MF_KINDS = (("divisions", "division"), ("logic_canons", "logic_canon"), ("corpora", "corpus"),
                ("workspaces", "workspace"), ("circuit_breaker_policies", "circuit_breaker_policy"),
                ("policies", "policy"), ("chatbots", "chatbot"))

    def _manifest(self, payload: dict):
        src = (payload or {}).get("manifest")
        if isinstance(src, str):
            try:
                src = json.loads(src)
            except ValueError:
                return None, (400, {"detail": {"message": "manifest could not be parsed",
                                               "issues": ["the fake platform accepts JSON manifests"]}})
        if not isinstance(src, dict):
            return None, (400, {"detail": "manifest required"})
        self.last_manifest = src
        return src, None

    def _flatten(self, m: dict) -> list[dict]:
        spec = m.get("spec") or {}
        out = []
        for section, kind in self.MF_KINDS:
            for item in spec.get(section) or []:
                if isinstance(item, dict) and item.get("id"):
                    out.append({"kind": kind, "logical_id": item["id"], "props": item})
        for rb in spec.get("roles") or []:
            lid = f"{rb.get('principal')}:{rb.get('role')}:{rb.get('scope', '*')}"
            out.append({"kind": "role_binding", "logical_id": lid, "props": rb})
        return out

    def _issues(self, m: dict) -> list[str]:
        issues = []
        if m.get("kind") != "SolutionManifest":
            issues.append("kind must be 'SolutionManifest'")
        meta = m.get("metadata") or {}
        if not meta.get("name"):
            issues.append("metadata.name is required")
        if meta.get("vendor") not in (None, "vd_1", "fake-vendor"):
            return [f"vendor {meta.get('vendor')!r} is not your vendor"]
        spec = m.get("spec") or {}
        ws_ids = {w.get("id") for w in spec.get("workspaces") or []}
        for w in spec.get("workspaces") or []:
            if w.get("engine") not in ("reasoning", "logic"):
                issues.append(f"workspace '{w.get('id')}': engine must be one of ['logic', 'reasoning']")
        for bp in spec.get("circuit_breaker_policies") or []:
            if bp.get("workspace") not in ws_ids:
                issues.append(f"circuit_breaker_policy '{bp.get('id')}': workspace '{bp.get('workspace')}' not declared")
            if bp.get("action") not in ("allow", "review", "block"):
                issues.append(f"circuit_breaker_policy '{bp.get('id')}': action must be one of ['allow', 'block', 'review']")
        for rp in spec.get("policies") or []:
            if rp.get("workspace") not in ws_ids:
                issues.append(f"policy '{rp.get('id')}': workspace '{rp.get('workspace')}' not declared")
        for cbt in spec.get("chatbots") or []:
            if not cbt.get("site_domain") and cbt.get("allow_any_origin") is not True:
                issues.append(f"chatbot '{cbt.get('id')}': site_domain is required")
        return issues

    def _guardrails(self, m: dict) -> list[str]:
        roles = (m.get("spec") or {}).get("roles") or []
        if not any(r.get("role") == "auditor" for r in roles):
            return ["guardrail: an 'auditor' role binding is required (segregation of duties)"]
        return []

    def _stack_row(self, stack: dict) -> dict:
        return {k: stack[k] for k in ("stack_id", "vendor_id", "name", "status", "current_version", "updated_at")}

    def mf_validate(self, payload: dict):
        m, err = self._manifest(payload)
        if err:
            return err
        issues = self._issues(m)
        meta = m.get("metadata") or {}
        out = {"ok": not issues, "issues": issues, "metadata": meta}
        if not issues:
            out["resources"] = [{"kind": r["kind"], "logical_id": r["logical_id"], "depends_on": []} for r in self._flatten(m)]
            out["expectations"] = list((m.get("spec") or {}).get("expectations") or [])
            out["guardrails"] = self._guardrails(m)
            out["lock"] = {"content_hash": "sha256:fake"}
            stack = self.stacks.get(meta.get("name"))
            out["stack"] = self._stack_row(stack) if stack else None
        return 200, out

    def _changes(self, stack: dict | None, m: dict) -> list[dict]:
        current = (stack or {}).get("resources") or {}
        desired = self._flatten(m)
        changes = []
        for r in desired:
            cur = current.get(r["logical_id"])
            if cur is None:
                changes.append({"action": "add", "kind": r["kind"], "logical_id": r["logical_id"]})
            elif cur["state"] != r["props"]:
                changed = sorted(k for k in set(cur["state"]) | set(r["props"]) if cur["state"].get(k) != r["props"].get(k))
                changes.append({"action": "update", "kind": r["kind"], "logical_id": r["logical_id"], "changed_props": changed})
            else:
                changes.append({"action": "no_change", "kind": r["kind"], "logical_id": r["logical_id"]})
        wanted = {r["logical_id"] for r in desired}
        for lid, cur in current.items():
            if lid not in wanted:
                changes.append({"action": "delete", "kind": cur["kind"], "logical_id": lid, "deletion_policy": "retain"})
        return changes

    def mf_plan(self, payload: dict):
        m, err = self._manifest(payload)
        if err:
            return err
        issues = self._issues(m)
        if issues:
            return 400, {"detail": {"message": "manifest is invalid", "issues": issues}}
        meta = m.get("metadata") or {}
        stack = self.stacks.get(meta.get("name"))
        changes = self._changes(stack, m)
        summary = {a: sum(1 for c in changes if c["action"] == a) for a in ("add", "update", "replace", "delete", "no_change")}
        return 200, {"stack_id": stack["stack_id"] if stack else "(unprovisioned)", "exists": bool(stack),
                     "from_version": stack["current_version"] if stack else None,
                     "to_version": (stack["current_version"] if stack else 0) + 1,
                     "summary": summary, "changes": changes,
                     "meter_impact": {"skus_touched": [], "apply_is_metered": True},
                     "evidence_impact": {"controls_gained": 0, "valuation_sources_added": 0},
                     "lock": {"content_hash": "sha256:fake"}, "guardrails": self._guardrails(m)}

    def _provision(self, stack: dict, r: dict) -> str:
        kind, lid, props = r["kind"], r["logical_id"], r["props"]
        existing = (stack["resources"].get(lid) or {}).get("physical_id")
        if kind == "division":
            return props.get("division_id") or f"dv_{lid}"
        if kind == "workspace":
            return props.get("workspace_id") or f"ws_{lid}"
        if kind == "logic_canon":
            base = existing.split("@")[0] if existing else f"lcanon_{lid}"
            version = 1 if not existing else (int(existing.split("@")[1]) +
                                              (1 if stack["resources"][lid]["state"].get("rulebook") != props.get("rulebook") else 0))
            return f"{base}@{version}"
        if kind == "circuit_breaker_policy":
            pid = existing or f"cbp_{uuid.uuid4().hex[:12]}"
            self.cb_policies[pid] = {"cb_policy_id": pid, "workspace_id": self.workspace_id, "name": props.get("name") or lid,
                                     "rules": props.get("rules") or [], "action": props.get("action"),
                                     "scope": props.get("scope") or "subject", "enabled": bool(props.get("enabled", True)),
                                     "description": props.get("description")}
            return pid
        if kind == "policy":
            pid = existing or f"pol_{uuid.uuid4().hex[:14]}"
            conds = props.get("when") if props.get("when") is not None else props.get("conditions")
            self.policies[pid] = {"policy_id": pid, "workspace_id": self.workspace_id, "name": props.get("name") or lid,
                                  "conditions": conds or [], "lane": props.get("lane"), "level": props.get("level"),
                                  "soul": props.get("soul") or "reasoning", "config": props.get("config") or {},
                                  "status": "active", "enabled": bool(props.get("enabled", True)),
                                  "description": props.get("description")}
            return pid
        if kind == "chatbot":
            eid = existing or f"emb_{uuid.uuid4().hex[:10]}"
            self.chatbots[eid] = {"embed_id": eid, "workspace_id": self.workspace_id, "division_id": self.division_id,
                                  "agent_name": props.get("agent_name"), "site_domain": props.get("site_domain") or "",
                                  "protected_action": props.get("protected_action"),
                                  "config": {**(props.get("config") or {}), "system_prompt": props.get("system_prompt")}}
            return eid
        return f"{kind}:{lid}"

    def _unprovision(self, stack: dict, lid: str) -> None:
        r = stack["resources"].get(lid) or {}
        pid = r.get("physical_id")
        if r.get("kind") == "circuit_breaker_policy":
            self.cb_policies.pop(pid, None)
        elif r.get("kind") == "policy":
            self.policies.pop(pid, None)
        elif r.get("kind") == "chatbot":
            self.chatbots.pop(pid, None)

    def _four_eyes(self, payload: dict):
        applied_by = (payload.get("applied_by") or "apikey:ak_1").strip()
        approved_by = (payload.get("approved_by") or "").strip() or None
        if not approved_by:
            return None, None, (409, {"detail": {"message": "a vendor guardrail refused this manifest", "reason": "guardrail",
                                                "violations": ["guardrail: an approver is required (maker-checker / four-eyes)"]}})
        if approved_by == applied_by:
            return None, None, (409, {"detail": {"message": f"author≠approver violated: applied_by and approved_by are both {applied_by!r}",
                                                "reason": "maker_checker"}})
        return applied_by, approved_by, None

    def mf_apply(self, payload: dict):
        if self.role != "tenant_admin":
            return 403, {"detail": "tenant_admin in this vendor required"}
        m, err = self._manifest(payload)
        if err:
            return err
        issues = self._issues(m)
        if issues:
            return 400, {"detail": {"message": "manifest is invalid", "issues": issues}}
        applied_by, approved_by, err = self._four_eyes(payload)
        if err:
            return err
        violations = self._guardrails(m)
        if violations:
            return 409, {"detail": {"message": "a vendor guardrail refused this manifest", "reason": "guardrail",
                                    "violations": violations}}
        meta = m.get("metadata") or {}
        stack = self.stacks.get(meta["name"])
        if stack is None:
            stack = self.stacks[meta["name"]] = {"stack_id": f"stack_{uuid.uuid4().hex[:12]}", "vendor_id": "vd_1",
                                                 "name": meta["name"], "status": "pending", "current_version": None,
                                                 "updated_at": "2026-09-27T12:00:00Z", "resources": {}, "versions": []}
        changes = self._changes(stack, m)
        actionable = [c for c in changes if c["action"] != "no_change"]
        if not actionable:
            return 200, {"status": "no_change", "idempotent": True, "version": stack["current_version"], "stack_id": stack["stack_id"]}
        desired = {r["logical_id"]: r for r in self._flatten(m)}
        applied = []
        for c in changes:
            if c["action"] == "delete":
                self._unprovision(stack, c["logical_id"])
                stack["resources"].pop(c["logical_id"], None)
            elif c["action"] != "no_change":
                r = desired[c["logical_id"]]
                stack["resources"][c["logical_id"]] = {"kind": r["kind"], "physical_id": self._provision(stack, r), "state": r["props"]}
            if c["action"] != "no_change":
                applied.append({"action": c["action"], "kind": c["kind"], "logical_id": c["logical_id"]})
        version = (stack["current_version"] or 0) + 1
        stack["versions"].insert(0, {"version_no": version, "applied_by": applied_by, "approved_by": approved_by,
                                     "ledger_anchor": str(uuid.uuid4()), "created_at": "2026-09-27T12:00:00Z"})
        stack.update({"current_version": version, "status": "active"})
        return 200, {"status": "applied", "version": version, "applied": applied, "stack_id": stack["stack_id"],
                     "ledger_anchor": stack["versions"][0]["ledger_anchor"],
                     "summary": {a: sum(1 for c in changes if c["action"] == a) for a in ("add", "update", "replace", "delete", "no_change")}}

    def mf_destroy(self, stack: dict, payload: dict):
        if self.role != "tenant_admin":
            return 403, {"detail": "tenant_admin in this vendor required"}
        applied_by, approved_by, err = self._four_eyes(payload or {})
        if err:
            return err
        applied = []
        for lid in list(stack["resources"]):
            if stack["resources"][lid]["kind"] != "role_binding":
                self._unprovision(stack, lid)
                applied.append({"action": "delete", "kind": stack["resources"][lid]["kind"], "logical_id": lid})
                del stack["resources"][lid]
        version = (stack["current_version"] or 0) + 1
        stack["versions"].insert(0, {"version_no": version, "applied_by": applied_by, "approved_by": approved_by,
                                     "ledger_anchor": str(uuid.uuid4()), "created_at": "2026-09-27T12:00:00Z"})
        stack["current_version"] = version
        return 200, {"status": "applied", "version": version, "applied": applied, "stack_id": stack["stack_id"], "retained": []}

    def mf_drift(self, stack: dict, payload: dict):
        p = payload or {}
        if (p.get("reconcile") or p.get("adopt")) and self.role != "tenant_admin":
            return 403, {"detail": "tenant_admin in this vendor required"}
        items = list(self.drift)
        out = {"stack_id": stack["stack_id"], "drifted": bool(items), "items": items}
        if items and p.get("reconcile"):
            out["reconciled"] = [it["logical_id"] for it in items]
            self.drift = []
        elif items and p.get("adopt"):
            out["adopted"] = [it["logical_id"] for it in items]
            self.drift = []
        return 200, out


def _cmp(op: str, a: float, b: float) -> bool:
    return {">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b, "==": a == b, "!=": a != b}[op]


class FakePlatformHTTP:
    """The same fake behind a real HTTP port, for subprocesses and for the
    CLI's own transport."""

    def __init__(self, fake: FakePlatform):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        transport = fake

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else None
                status, headers, raw = transport(self.command, f"http://fake{self.path}",
                                                 {k.lower(): v for k, v in self.headers.items()}, body, 10.0)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakePlatformHTTP":
        import threading
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

