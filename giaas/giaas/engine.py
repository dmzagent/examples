"""plan · apply · verify · destroy — reconciling a declaration with the platform.

The engine reads what the workspace holds today, compares it with what the
governance file declares, and produces a change set. `apply` executes the
change set in dependency order and records the ids the platform assigned in
a local state file (never secrets) and the application's env file (the one
place a minted key is written).

Identity is by name: a breaker policy called "Block on prompt injection" is
the same policy on every run, however many times you apply. Renaming a
resource in the file therefore creates a new one and leaves the old; use
`destroy` (or the console) to retire it.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .client import Platform, PlatformError, fingerprint
from .resources import (
    BreakerPolicy, CanonRequirement, Chatbot, DivisionConfig, Expectation, Governance,
    LogicRulebook, Policy, Ref, Resource, SdkKey, canonical_rulebook,
)

ORDER = ("canon", "division_config", "breaker_policy", "policy", "chatbot",
         "logic_rulebook", "sdk_key")

SYMBOL = {"create": "+", "update": "~", "delete": "-", "noop": "=", "requires": "!",
          "skip": "·", "error": "x"}


class RequirementUnmet(RuntimeError):
    """A declared requirement is not satisfied and the run must not go on."""


@dataclass
class Change:
    action: str
    resource: Resource
    summary: str
    before: Any = None
    after: Any = None
    execute: Callable[[], dict] | None = None
    result: dict | None = None
    error: str | None = None

    @property
    def symbol(self) -> str:
        return SYMBOL.get(self.action, "?")


@dataclass
class Ctx:
    platform: Platform
    workspace_id: str
    division_id: str
    role: str
    user_id: str
    state: dict
    state_path: Path
    env_file: Path
    options: dict = field(default_factory=dict)
    log: Callable[[str], None] = lambda msg: None


# ------------------------------------------------------------------------- #
# Connecting
# ------------------------------------------------------------------------- #


def connect(gov: Governance, gov_path: Path, *, platform: Platform | None = None,
            env_file: Path | None = None, options: dict | None = None,
            log: Callable[[str], None] | None = None) -> Ctx:
    """Resolve the key's workspace and division and load local state."""
    log = log or (lambda msg: None)
    platform = platform or Platform(log=log)
    who = platform.whoami()
    division_id = platform.division_id_for_key()
    state_path = gov_path.parent / ".giaas" / f"{gov.name}.state.json"
    state = _load_json(state_path) or {}
    stale = state.get("workspace_id") and state["workspace_id"] != who["workspace_id"]
    if stale:
        log(f"state file was written for workspace {state['workspace_id']}; "
            f"the key is bound to {who['workspace_id']} — starting fresh state")
        state = {}
    state.update({"governance": gov.name, "base_url": platform.base_url,
                  "workspace_id": who["workspace_id"], "division_id": division_id})
    return Ctx(platform=platform, workspace_id=who["workspace_id"], division_id=division_id,
               role=who["role"], user_id=who["user_id"] or "", state=state,
               state_path=state_path, env_file=env_file or (gov_path.parent / ".env"),
               options=dict(options or {}), log=log)


# ------------------------------------------------------------------------- #
# Plan
# ------------------------------------------------------------------------- #


def plan(gov: Governance, ctx: Ctx) -> list[Change]:
    """Read the workspace and compute the change set. Read-only."""
    changes: list[Change] = []
    for kind in ORDER:
        for resource in gov.by_kind(kind):
            handler = HANDLERS[kind]
            try:
                changes.append(handler.plan(ctx, resource))
            except PlatformError as exc:
                changes.append(Change("error", resource, f"could not read: {exc}"))
    return changes


def apply(gov: Governance, ctx: Ctx, changes: list[Change]) -> list[Change]:
    """Execute a change set in order, then write state and the env file.

    Requirements come first and, unless the run allows it, an unmet one stops
    the apply before anything is created — a policy on a tag the workspace's
    vocabulary does not contain is a policy that never fires.
    """
    if ctx.role != "tenant_admin":
        raise RequirementUnmet(
            f"applying needs a tenant_admin key; this key holds the {ctx.role} role")
    unmet: list[str] = []
    for change in changes:
        if change.action in ("noop", "skip"):
            continue
        if change.action == "error":
            raise RequirementUnmet(change.summary)
        try:
            change.result = change.execute() if change.execute else {}
        except PlatformError as exc:
            change.error = str(exc)
            change.action = "error"
            raise
        if change.result.get("unmet"):
            unmet.append(f"{change.resource.ident}: {change.result.get('reason')}")
            if not ctx.options.get("allow_missing_requirements"):
                raise RequirementUnmet(
                    "\n".join(unmet) + "\n\nRe-run once the requirement is met, or pass "
                    "--allow-missing-requirements to apply the rest anyway.")
        change.resource.outputs.update(change.result)
        _remember(ctx, change.resource)
    write_env(gov, ctx)
    save_state(ctx)
    return changes


def destroy(gov: Governance, ctx: Ctx) -> list[Change]:
    """Delete what the declaration created, most dependent first."""
    if ctx.role != "tenant_admin":
        raise RequirementUnmet(
            f"destroying needs a tenant_admin key; this key holds the {ctx.role} role")
    changes: list[Change] = []
    for kind in reversed(ORDER):
        for resource in gov.by_kind(kind):
            change = HANDLERS[kind].destroy(ctx, resource)
            if change.execute:
                try:
                    change.result = change.execute()
                except PlatformError as exc:
                    change.action, change.error = "error", str(exc)
            ctx.state.get("resources", {}).pop(resource.ident, None)
            changes.append(change)
    save_state(ctx)
    return changes


# ------------------------------------------------------------------------- #
# Verify — requirements + expectations, using the platform's own evaluator
# ------------------------------------------------------------------------- #


def verify(gov: Governance, ctx: Ctx) -> list[dict]:
    results: list[dict] = []
    for req in gov.by_kind("canon"):
        change = HANDLERS["canon"].plan(ctx, req)
        results.append({"kind": "requirement", "name": req.canon_id,
                        "ok": change.action == "noop", "detail": change.summary})
    for exp in gov.expectations:
        results.append(_check_expectation(ctx, exp))
    return results


def _check_expectation(ctx: Ctx, exp: Expectation) -> dict:
    body = {"workspace_id": ctx.workspace_id, "soul": exp.soul,
            "labels": exp.labels, "fired": exp.fired}
    try:
        out = ctx.platform.post("/v1/policies/evaluate", body)
    except PlatformError as exc:
        return {"kind": "expectation", "name": exp.name, "ok": False, "detail": str(exc)}
    resolved = out.get("resolved") or {}
    got = {lane: _level_of(resolved.get(lane)) for lane in ("enforce", "coordinate", "remediate")}
    fired_names = sorted({d.get("name") for d in out.get("decisions") or [] if d.get("name")})
    problems: list[str] = []
    for lane in ("enforce", "coordinate", "remediate"):
        want = getattr(exp, lane)
        if want is not None and got[lane] != want:
            problems.append(f"{lane}: expected {want}, engine resolved {got[lane]}")
    for name in exp.policies:
        if name not in fired_names:
            problems.append(f"policy {name!r} did not fire (fired: {fired_names or 'none'})")
    detail = ("; ".join(problems) if problems
              else "resolved " + ", ".join(f"{k}={v}" for k, v in got.items() if v)
              + (f"; fired {fired_names}" if fired_names else "; nothing fired"))
    return {"kind": "expectation", "name": exp.name, "ok": not problems, "detail": detail}


def _level_of(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("level") or value.get("action")
    if isinstance(value, list):
        return ",".join(sorted(_level_of(v) or "" for v in value)) or None
    return str(value)


# ------------------------------------------------------------------------- #
# State and env
# ------------------------------------------------------------------------- #


def _remember(ctx: Ctx, resource: Resource) -> None:
    public = {k: v for k, v in resource.outputs.items()
              if k not in ("secret",) and not str(k).endswith("_secret")}
    ctx.state.setdefault("resources", {})[resource.ident] = public


def save_state(ctx: Ctx) -> None:
    ctx.state["updated_at"] = _now()
    ctx.state_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.state_path.write_text(json.dumps(ctx.state, indent=2, sort_keys=True) + "\n")
    gitignore = ctx.state_path.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")


def write_env(gov: Governance, ctx: Ctx) -> dict[str, str]:
    values = {"DMZAGENT_BASE_URL": ctx.platform.base_url,
              "DMZAGENT_WORKSPACE_ID": ctx.workspace_id,
              "DMZAGENT_DIVISION_ID": ctx.division_id}
    for var, ref in gov.env_outputs.items():
        try:
            values[var] = str(ref.resolve() if isinstance(ref, Ref) else ref)
        except KeyError as exc:
            ctx.log(f"env {var}: {exc}")
    for var, value in values.items():
        set_env_var(ctx.env_file, var, value)
    return values


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def set_env_var(path: Path, name: str, value: str) -> None:
    """Set NAME=value in a dotenv file, replacing an existing line. The file
    is created mode 0600 because it may hold a key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    rendered = f"{name}={_quote(value)}"
    for i, line in enumerate(lines):
        m = _ENV_LINE.match(line)
        if m and m.group(1) == name:
            lines[i] = rendered
            break
    else:
        lines.append(rendered)
    if not path.exists():
        path.touch(mode=0o600)
    path.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_env_var(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        m = _ENV_LINE.match(line)
        if m and m.group(1) == name:
            raw = line.split("=", 1)[1].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            return raw
    return None


def _quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@+=,-]*", value):
        return value
    return json.dumps(value)


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _eq(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def _text(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) or value is None else str(value)


# ------------------------------------------------------------------------- #
# Handlers
# ------------------------------------------------------------------------- #


class DivisionConfigHandler:
    @staticmethod
    def plan(ctx: Ctx, res: DivisionConfig) -> Change:
        path = f"/v1/divisions/{ctx.division_id}/config"
        current = ctx.platform.get(path).get("config") or {}
        diffs = {k: (current.get(k), v) for k, v in res.settings.items()
                 if not _eq(current.get(k), v)}
        if not diffs:
            res.outputs.update({"config": current})
            return Change("noop", res, "settings already in place: "
                          + ", ".join(f"{k}={v}" for k, v in res.settings.items()), current, current)
        merged = {**current, **res.settings}
        summary = "; ".join(f"{k}: {b if b is not None else '(unset)'} → {a}" for k, (b, a) in diffs.items())

        def execute() -> dict:
            out = ctx.platform.put(path, {"config": merged})
            return {"config": out.get("config", merged)}
        return Change("update", res, summary, current, merged, execute)

    @staticmethod
    def destroy(ctx: Ctx, res: DivisionConfig) -> Change:
        return Change("skip", res, "division settings are left as they are")


class CanonHandler:
    @staticmethod
    def installed(ctx: Ctx) -> dict[str, dict]:
        result = ctx.platform.rpc("resources/read", {"uri": "concordia:/workspace/canons"})
        text = ((result or {}).get("contents") or [{}])[0].get("text") or "{}"
        canons = json.loads(text).get("canons") or []
        return {c.get("canon_id"): c for c in canons}

    @staticmethod
    def plan(ctx: Ctx, res: CanonRequirement) -> Change:
        have = CanonHandler.installed(ctx).get(res.canon_id)
        if have and have.get("enabled", True):
            version = have.get("latest_version")
            if res.version and version != res.version:
                return Change("requires", res,
                              f"installed at {version}, declaration wants {res.version} — "
                              "update it in the console (Library)", have, None,
                              lambda: {"installed": True, "version": version, "unmet": True,
                                       "reason": f"version {version} != {res.version}"})
            res.outputs.update({"installed": True, "version": version})
            return Change("noop", res, f"installed ({have.get('name')} {version})", have, have)

        why = f" — {res.why}" if res.why else ""
        instructions = (f"not installed{why}. API keys cannot install Canons; in the console "
                        f"open Library, find {res.canon_id} and install it into this workspace")

        def execute() -> dict:
            body = {"workspace_id": ctx.workspace_id, "canon_id": res.canon_id}
            if res.version:
                body["version"] = res.version
            try:
                out = ctx.platform.post("/v1/corpus/install", body)
            except PlatformError as exc:
                if exc.status in (401, 403):
                    return {"installed": False, "unmet": True, "reason": instructions}
                raise
            return {"installed": True, "version": out.get("installed_version") or res.version}
        return Change("requires", res, instructions + " (apply will try first)", None, None, execute)

    @staticmethod
    def destroy(ctx: Ctx, res: CanonRequirement) -> Change:
        return Change("skip", res, "Canon installs are managed in the console")


class BreakerPolicyHandler:
    @staticmethod
    def existing(ctx: Ctx) -> dict[str, dict]:
        rows: list[dict] = []
        cursor = None
        while True:
            page = ctx.platform.get("/v1/cb/policies", workspace_id=ctx.workspace_id,
                                    limit=500, cursor=cursor)
            rows.extend(page.get("policies") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return {r.get("name"): r for r in rows}

    @staticmethod
    def _norm(row: dict) -> dict:
        rules = sorted(({"tag": r.get("tag"), "op": r.get("op"), "value": float(r.get("value"))}
                        for r in row.get("rules") or []), key=lambda r: (r["tag"], r["op"], r["value"]))
        return {"rules": rules, "action": row.get("action"), "scope": row.get("scope") or "subject",
                "enabled": bool(row.get("enabled", True)), "description": _text(row.get("description"))}

    @staticmethod
    def plan(ctx: Ctx, res: BreakerPolicy) -> Change:
        current = BreakerPolicyHandler.existing(ctx).get(res.name)
        desired = res.desired()
        body = {"workspace_id": ctx.workspace_id, **desired}
        label = f"{len(res.rules)} clause{'s' if len(res.rules) != 1 else ''} → {res.action}"
        if current and _eq(BreakerPolicyHandler._norm(current), BreakerPolicyHandler._norm(desired)):
            res.outputs.update({"cb_policy_id": current["cb_policy_id"]})
            return Change("noop", res, f"unchanged ({label})", current, current)

        def execute() -> dict:
            payload = dict(body)
            if current:
                payload["cb_policy_id"] = current["cb_policy_id"]
            out = ctx.platform.post("/v1/cb/policies", payload)
            return {"cb_policy_id": out.get("cb_policy_id")}
        if current:
            return Change("update", res, f"rules or action changed ({label})", current, desired, execute)
        return Change("create", res, label, None, desired, execute)

    @staticmethod
    def destroy(ctx: Ctx, res: BreakerPolicy) -> Change:
        current = BreakerPolicyHandler.existing(ctx).get(res.name)
        if not current:
            return Change("noop", res, "already absent")
        pid = current["cb_policy_id"]
        return Change("delete", res, pid, current, None,
                      lambda: ctx.platform.delete(f"/v1/cb/policies/{pid}") or {})


class PolicyHandler:
    @staticmethod
    def existing(ctx: Ctx) -> dict[str, dict]:
        rows = ctx.platform.get("/v1/policies", workspace_id=ctx.workspace_id).get("policies") or []
        return {r.get("name"): r for r in rows}

    @staticmethod
    def _norm(row: dict) -> dict:
        conds = []
        for c in row.get("conditions") or []:
            item = {"kind": c.get("kind"), "label": c.get("label")}
            if c.get("kind") == "strength":
                item["op"] = c.get("op") or ">="
                item["threshold"] = float(c.get("threshold")) if c.get("threshold") is not None else None
            conds.append(item)
        conds.sort(key=lambda c: json.dumps(c, sort_keys=True))
        return {"conditions": conds, "lane": row.get("lane"), "level": row.get("level"),
                "soul": row.get("soul") or "reasoning", "config": row.get("config") or {},
                "enabled": bool(row.get("enabled", True)), "description": _text(row.get("description"))}

    @staticmethod
    def plan(ctx: Ctx, res: Policy) -> Change:
        current = PolicyHandler.existing(ctx).get(res.name)
        desired = res.desired()
        label = f"{res.lane}" + (f"/{res.level}" if res.level else "") + f" on {res.soul} soul"
        if current and _eq(PolicyHandler._norm(current), PolicyHandler._norm(desired)):
            res.outputs.update({"policy_id": current["policy_id"]})
            return Change("noop", res, f"unchanged ({label})", current, current)

        def execute() -> dict:
            payload = {"workspace_id": ctx.workspace_id, **desired, "status": "active"}
            if current:
                payload["policy_id"] = current["policy_id"]
            out = ctx.platform.post("/v1/policies", payload)
            return {"policy_id": out.get("policy_id")}
        if current:
            return Change("update", res, f"conditions or response changed ({label})", current, desired, execute)
        return Change("create", res, label, None, desired, execute)

    @staticmethod
    def destroy(ctx: Ctx, res: Policy) -> Change:
        current = PolicyHandler.existing(ctx).get(res.name)
        if not current:
            return Change("noop", res, "already absent")
        pid = current["policy_id"]
        return Change("delete", res, pid, current, None,
                      lambda: ctx.platform.delete(f"/v1/policies/{pid}") or {})


class ChatbotHandler:
    @staticmethod
    def existing(ctx: Ctx) -> dict[str, dict]:
        rows = ctx.platform.get("/v1/chatbot-definitions", division_id=ctx.division_id)
        return {r.get("agent_name"): r for r in (rows or []) if not r.get("revoked_at")}

    @staticmethod
    def outputs_for(ctx: Ctx, row: dict) -> dict:
        embed_id = row["embed_id"]
        base = ctx.platform.base_url
        return {"embed_id": embed_id, "division_id": row.get("division_id") or ctx.division_id,
                "workspace_id": row.get("workspace_id") or ctx.workspace_id,
                "agent_subject_id": f"subject:{row.get('division_id') or ctx.division_id}:chat-agent:{embed_id}",
                "embed_script_url": f"{base}/v1/embed/chat.js",
                "embed_chat_url": f"{base}/v1/embed/{embed_id}/chat",
                "connector_instance_id": row.get("connector_instance_id")}

    @staticmethod
    def plan(ctx: Ctx, res: Chatbot) -> Change:
        current = ChatbotHandler.existing(ctx).get(res.name)
        desired = res.desired()
        origin = res.site_domain or "any origin (development)"
        if current:
            same = (_text(current.get("site_domain")) == _text(res.site_domain)
                    and _text(current.get("protected_action")) == _text(res.protected_action)
                    and _eq(current.get("config") or {}, res.config))
            if same:
                res.outputs.update(ChatbotHandler.outputs_for(ctx, current))
                return Change("noop", res, f"unchanged (embed {current['embed_id']}, site {origin})", current, current)

            def update() -> dict:
                out = ctx.platform.patch(f"/v1/chatbot-definitions/{current['embed_id']}",
                                         {"site_domain": res.site_domain,
                                          "protected_action": res.protected_action,
                                          "config": res.config})
                return ChatbotHandler.outputs_for(ctx, out)
            return Change("update", res, f"definition changed (embed {current['embed_id']})", current, desired, update)

        def create() -> dict:
            out = ctx.platform.post("/v1/chatbot-definitions",
                                    {"workspace_id": ctx.workspace_id, **desired})
            return ChatbotHandler.outputs_for(ctx, out)
        return Change("create", res, f"hosted chat agent for {origin}", None, desired, create)

    @staticmethod
    def destroy(ctx: Ctx, res: Chatbot) -> Change:
        current = ChatbotHandler.existing(ctx).get(res.name)
        if not current:
            return Change("noop", res, "already absent")
        embed_id = current["embed_id"]
        return Change("delete", res, f"revoke embed {embed_id}", current, None,
                      lambda: ctx.platform.delete(f"/v1/chatbot-definitions/{embed_id}") or {})


class LogicRulebookHandler:
    @staticmethod
    def existing(ctx: Ctx, slug: str) -> dict | None:
        rows = ctx.platform.get("/v1/logic-canons").get("logic_canons") or []
        return next((r for r in rows if r.get("slug") == slug), None)

    @staticmethod
    def published_rulebook(ctx: Ctx, canon_id: str, version: int) -> dict:
        row = ctx.platform.get(f"/v1/logic-canons/{canon_id}/versions/{version}")
        doc = row.get("rulebook")
        if doc is None and row.get("rulebook_json"):
            doc = json.loads(row["rulebook_json"])
        return canonical_rulebook(doc or {})

    @staticmethod
    def installed_version(ctx: Ctx, canon_id: str) -> int | None:
        rows = ctx.platform.get(f"/v1/workspaces/{ctx.workspace_id}/logic-canons").get("installs") or []
        for r in rows:
            if r.get("logic_canon_id") == canon_id:
                return r.get("version")
        return None

    @staticmethod
    def outputs_for(res: LogicRulebook, canon_id: str, version: int, bridged: Any = None) -> dict:
        rules = res.rulebook.get("rules") or []
        return {"logic_canon_id": canon_id, "version": version,
                "rule_labels": {r["id"]: f"{res.slug}@{version}:{r['id']}" for r in rules if r.get("id")},
                "bridged_policies": bridged}

    @staticmethod
    def plan(ctx: Ctx, res: LogicRulebook) -> Change:
        current = LogicRulebookHandler.existing(ctx, res.slug)
        desired = canonical_rulebook(res.rulebook)
        n = len(res.rulebook.get("rules") or [])
        label = f"{n} rule{'s' if n != 1 else ''}"
        if current and current.get("latest_version"):
            canon_id, latest = current["logic_canon_id"], int(current["latest_version"])
            published = LogicRulebookHandler.published_rulebook(ctx, canon_id, latest)
            installed = LogicRulebookHandler.installed_version(ctx, canon_id)
            if _eq(published, desired):
                if installed == latest:
                    res.outputs.update(LogicRulebookHandler.outputs_for(res, canon_id, latest))
                    return Change("noop", res, f"version {latest} published and installed ({label})", current, current)

                def install() -> dict:
                    out = ctx.platform.post(f"/v1/logic-canons/{canon_id}/install",
                                            {"workspace_id": ctx.workspace_id, "version": latest})
                    return LogicRulebookHandler.outputs_for(res, canon_id, latest, out.get("bridged_policies"))
                return Change("update", res, f"version {latest} published but not installed here", current, desired, install)

            def republish() -> dict:
                ver = ctx.platform.post(f"/v1/logic-canons/{canon_id}/versions",
                                        {"rulebook": res.rulebook, "changelog": "giaas apply"})
                new_version = int(ver.get("version"))
                out = ctx.platform.post(f"/v1/logic-canons/{canon_id}/install",
                                        {"workspace_id": ctx.workspace_id, "version": new_version})
                return LogicRulebookHandler.outputs_for(res, canon_id, new_version, out.get("bridged_policies"))
            return Change("update", res, f"rules changed → publish version {latest + 1} and install", current, desired, republish)

        def create() -> dict:
            canon = current or ctx.platform.post("/v1/logic-canons", {"name": res.name, "slug": res.slug,
                                                                       "description": res.description})
            canon_id = canon["logic_canon_id"]
            ver = ctx.platform.post(f"/v1/logic-canons/{canon_id}/versions",
                                    {"rulebook": res.rulebook, "changelog": "giaas apply"})
            new_version = int(ver.get("version"))
            out = ctx.platform.post(f"/v1/logic-canons/{canon_id}/install",
                                    {"workspace_id": ctx.workspace_id, "version": new_version})
            return LogicRulebookHandler.outputs_for(res, canon_id, new_version, out.get("bridged_policies"))
        return Change("create", res, f"publish version 1 and install ({label})", None, desired, create)

    @staticmethod
    def destroy(ctx: Ctx, res: LogicRulebook) -> Change:
        current = LogicRulebookHandler.existing(ctx, res.slug)
        if not current:
            return Change("noop", res, "already absent")
        canon_id = current["logic_canon_id"]

        def remove() -> dict:
            try:
                ctx.platform.delete(f"/v1/logic-canons/{canon_id}/install/{ctx.workspace_id}")
            except PlatformError as exc:
                if exc.status != 404:
                    raise
            ctx.platform.post(f"/v1/logic-canons/{canon_id}/unpublish", {})
            return {}
        return Change("delete", res, f"uninstall and unpublish {canon_id}", current, None, remove)


class SdkKeyHandler:
    @staticmethod
    def plan(ctx: Ctx, res: SdkKey) -> Change:
        record = ctx.state.get("sdk_keys", {}).get(res.label)
        in_env = read_env_var(ctx.env_file, res.env_var)
        if record and in_env and in_env.startswith("ck_") and not ctx.options.get("rotate_keys"):
            res.outputs.update({"prefix": record.get("prefix"), "env_var": res.env_var})
            return Change("noop", res, f"minted {record.get('minted_at', '')} ({record.get('prefix')}), "
                          f"in {ctx.env_file.name} as {res.env_var}", record, record)

        def mint() -> dict:
            out = ctx.platform.post("/v1/agent-stream/api-keys",
                                    {"workspace_id": ctx.workspace_id, "label": res.label})
            secret = out.get("key") or ""
            set_env_var(ctx.env_file, res.env_var, secret)
            entry = {"prefix": fingerprint(secret), "minted_at": _now(), "env_var": res.env_var}
            ctx.state.setdefault("sdk_keys", {})[res.label] = entry
            return {"prefix": entry["prefix"], "env_var": res.env_var}
        verb = "rotate" if record else "mint"
        return Change("create", res, f"{verb} an analyst key → {ctx.env_file.name} as {res.env_var}", record, None, mint)

    @staticmethod
    def destroy(ctx: Ctx, res: SdkKey) -> Change:
        record = ctx.state.get("sdk_keys", {}).pop(res.label, None)
        note = "keys are revoked in the console (Team & access); removed from local state"
        return Change("skip", res, note if record else "nothing minted here", record, None)


HANDLERS = {
    "division_config": DivisionConfigHandler,
    "canon": CanonHandler,
    "breaker_policy": BreakerPolicyHandler,
    "policy": PolicyHandler,
    "chatbot": ChatbotHandler,
    "logic_rulebook": LogicRulebookHandler,
    "sdk_key": SdkKeyHandler,
}
