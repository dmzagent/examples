"""The Solution Manifest wheel from the client side.

A manifest is a YAML (or JSON) file the platform validates, plans and
applies server-side: the platform owns the Stack, computes the Change Set,
enforces maker-checker and the vendor's guardrails, and anchors every
version on the ledger. This module loads the file, fills the placeholders a
key can fill, submits it, and renders what came back. It never parses YAML
itself: the platform's validate answer carries the metadata and the
expectations a command needs.

Placeholders: ``${NAME}`` and ``${NAME:-default}``. NAME is filled from
``--var NAME=value`` and from the key's own context (DMZAGENT_VENDOR,
DMZAGENT_WORKSPACE_ID, DMZAGENT_DIVISION_ID, DMZAGENT_BASE_URL). Anything
else is left exactly as written, so a secret reference such as
``${ACME_LICENSE_KEY}`` reaches the platform as a reference, never a value.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import ui
from .client import ConfigError, PlatformError
from .envfile import set_env_var

DEFAULT_FILES = ("solution.yaml", "solution.yml", "solution.json")
CONTEXT_VARS = ("DMZAGENT_BASE_URL", "DMZAGENT_VENDOR", "DMZAGENT_WORKSPACE_ID", "DMZAGENT_DIVISION_ID")
ENV_KINDS = ("division", "workspace", "chatbot", "logic_canon", "circuit_breaker_policy", "policy")
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
SYMBOL = {"add": "+", "update": "~", "replace": "±", "delete": "-", "no_change": "="}


# -- the file ----------------------------------------------------------------

def find_file(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise ConfigError(f"no such manifest: {path}")
        return path
    for name in DEFAULT_FILES:
        if Path(name).exists():
            return Path(name)
    raise ConfigError("no solution.yaml here; pass the manifest path, or `dmz init <template>` writes one")


def substitute(text: str, values: dict[str, str]) -> str:
    def fill(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if name in values and values[name] is not None:
            return str(values[name])
        if default is not None:
            return default
        return m.group(0)                              # a reference stays a reference
    return _PLACEHOLDER.sub(fill, text)


def context_values(ctx, text: str) -> dict[str, str]:
    """The key's own facts, fetched only when the text asks for them."""
    values: dict[str, str] = {"DMZAGENT_BASE_URL": ctx.base_url}
    if "${DMZAGENT_" in text and any(v in text for v in CONTEXT_VARS[1:]):
        who = ctx.whoami()
        values.update({"DMZAGENT_VENDOR": who.get("vendor_id") or "",
                       "DMZAGENT_WORKSPACE_ID": who.get("workspace_id") or "",
                       "DMZAGENT_DIVISION_ID": who.get("division_id") or ""})
    return values


def load(ctx, path: Path, variables: dict[str, str]) -> str:
    text = path.read_text()
    values = {**context_values(ctx, text), **variables}
    return substitute(text, values)


def unresolved(text: str) -> list[str]:
    """Placeholders still in the text that look like ours (a hint, not an error)."""
    return sorted({m.group(1) for m in _PLACEHOLDER.finditer(text)
                   if m.group(1).startswith("DMZAGENT_") and m.group(2) is None})


# -- the platform's manifest surface ---------------------------------------

def validate(ctx, text: str) -> dict:
    return ctx.platform.post("/v1/manifests/validate", {"manifest": text})


def plan(ctx, text: str) -> dict:
    return ctx.platform.post("/v1/manifests/plan", {"manifest": text})


def apply(ctx, text: str, *, applied_by: str | None, approved_by: str | None) -> dict:
    body: dict[str, Any] = {"manifest": text}
    if applied_by:
        body["applied_by"] = applied_by
    if approved_by:
        body["approved_by"] = approved_by
    return ctx.platform.post("/v1/manifests/apply", body)


def destroy(ctx, stack_id: str, *, applied_by: str | None, approved_by: str | None) -> dict:
    body: dict[str, Any] = {}
    if applied_by:
        body["applied_by"] = applied_by
    if approved_by:
        body["approved_by"] = approved_by
    return ctx.platform.post(f"/v1/stacks/{stack_id}/destroy", body)


def drift(ctx, stack_id: str, *, reconcile: bool = False, adopt: bool = False) -> dict:
    return ctx.platform.post(f"/v1/stacks/{stack_id}/drift", {"reconcile": reconcile, "adopt": adopt})


def stacks(ctx) -> list[dict]:
    return list(ctx.platform.get("/v1/stacks").get("stacks") or [])


def stack(ctx, stack_id: str) -> dict:
    return ctx.platform.get(f"/v1/stacks/{stack_id}")


def stack_by_name(ctx, name: str) -> dict | None:
    return next((s for s in stacks(ctx) if s.get("name") == name), None)


def physical_ids(detail: dict) -> dict[str, str]:
    return {r["logical_id"]: r.get("physical_id") for r in detail.get("resources") or [] if r.get("physical_id")}


# -- rendering ---------------------------------------------------------------

def render_validation(result: dict) -> str:
    lines = []
    if result.get("ok"):
        meta = result.get("metadata") or {}
        lines.append(ui.ok("valid") + f"  {meta.get('name', '')} v{meta.get('version', '?')}  "
                     + ui.dim(f"{len(result.get('resources') or [])} resources, "
                              f"{len(result.get('expectations') or [])} expectations"))
    else:
        lines.append(ui.fail("invalid"))
        for issue in result.get("issues") or []:
            lines.append(f"  {ui.fail('x')} {issue}")
    for v in result.get("guardrails") or []:
        lines.append(f"  {ui.warn('!')} {v}")
    return "\n".join(lines)


def render_plan(result: dict, *, show_unchanged: bool = False) -> str:
    s = result.get("summary") or {}
    head = (f"Plan: {s.get('add', 0)} to add, {s.get('update', 0)} to change, {s.get('replace', 0)} to replace, "
            f"{s.get('delete', 0)} to remove, {s.get('no_change', 0)} unchanged")
    lines = [ui.bold(head), ui.dim(f"stack {result.get('stack_id')}" + ("" if result.get("exists") else " (new)")
                                    + f" · version {result.get('from_version') or 0} → {result.get('to_version')}")]
    for c in result.get("changes") or []:
        action = c.get("action")
        if action == "no_change" and not show_unchanged:
            continue
        colour = {"add": ui.ok, "update": ui.warn, "replace": ui.warn, "delete": ui.fail}.get(action, ui.dim)
        extra = ""
        if c.get("replace_because"):
            extra = ui.dim(f"  replace: {', '.join(c['replace_because'])}")
        elif c.get("cascade_from"):
            extra = ui.dim(f"  re-applied: {', '.join(c['cascade_from'])} changed")
        elif c.get("changed_props"):
            extra = ui.dim(f"  {', '.join(c['changed_props'])}")
        elif c.get("deletion_policy"):
            extra = ui.dim(f"  {c['deletion_policy']}")
        lines.append(f"  {colour(SYMBOL.get(action, '?'))} {c.get('kind', ''):<24} {c.get('logical_id', '')}{extra}")
    mi, ei = result.get("meter_impact") or {}, result.get("evidence_impact") or {}
    lines.append(ui.dim(f"meter: {', '.join(mi.get('skus_touched') or []) or 'no new SKUs'} · "
                        f"evidence: +{ei.get('controls_gained', 0)} controls, +{ei.get('valuation_sources_added', 0)} valuation sources"))
    for v in result.get("guardrails") or []:
        lines.append(f"  {ui.warn('!')} {v}")
    return "\n".join(lines)


def render_plan_markdown(result: dict, name: str) -> str:
    s = result.get("summary") or {}
    lines = [f"### Solution Manifest plan · `{name}`",
             f"stack `{result.get('stack_id')}` · add {s.get('add', 0)} · update {s.get('update', 0)} · "
             f"replace {s.get('replace', 0)} · delete {s.get('delete', 0)} · unchanged {s.get('no_change', 0)}", ""]
    for c in result.get("changes") or []:
        if c.get("action") == "no_change":
            continue
        extra = ""
        if c.get("replace_because"):
            extra = f" (replace: {', '.join(c['replace_because'])})"
        elif c.get("cascade_from"):
            extra = f" (re-applied: {', '.join(c['cascade_from'])} changed)"
        elif c.get("changed_props"):
            extra = f" ({', '.join(c['changed_props'])})"
        lines.append(f"- **{c.get('action')}** {c.get('kind')}/{c.get('logical_id')}{extra}")
    mi, ei = result.get("meter_impact") or {}, result.get("evidence_impact") or {}
    lines += ["", f"_meter_: {', '.join(mi.get('skus_touched') or []) or '—'} · "
                  f"_evidence_: +{ei.get('controls_gained', 0)} controls, +{ei.get('valuation_sources_added', 0)} valuation sources"]
    for v in result.get("guardrails") or []:
        lines.append(f"- :warning: {v}")
    return "\n".join(lines)


def render_apply(result: dict) -> str:
    status = result.get("status")
    if status == "no_change":
        return ui.ok("no change") + ui.dim(f"  stack {result.get('stack_id')} stays at version {result.get('version')}")
    lines = [ui.ok("applied") + f"  version {result.get('version')}" + ui.dim(f"  stack {result.get('stack_id')}")]
    for a in result.get("applied") or []:
        colour = {"add": ui.ok, "update": ui.warn, "replace": ui.warn, "delete": ui.fail}.get(a.get("action"), ui.dim)
        lines.append(f"  {colour(SYMBOL.get(a.get('action'), '?'))} {a.get('kind', ''):<24} {a.get('logical_id', '')}")
    if result.get("ledger_anchor"):
        lines.append(ui.dim(f"  ledger {result['ledger_anchor']}"))
    return "\n".join(lines)


def render_stack(detail: dict) -> str:
    lines = [ui.bold(detail.get("name", "")) + f"  {ui.dim(detail.get('stack_id', ''))}  "
             + ui.breaker("closed") if False else ui.bold(detail.get("name", "")) + "  " + ui.dim(detail.get("stack_id", ""))
             + f"  status {detail.get('status')}  version {detail.get('current_version')}"]
    lines.append(ui.table(["kind", "logical id", "physical id", "policy"],
                          [(r.get("kind"), r.get("logical_id"), r.get("physical_id") or ui.dim("—"), r.get("deletion_policy"))
                           for r in detail.get("resources") or []]))
    versions = detail.get("versions") or []
    if versions:
        lines.append("")
        lines.append(ui.table(["version", "applied by", "approved by", "ledger", "at"],
                              [(v.get("version_no"), v.get("applied_by"), v.get("approved_by") or ui.dim("—"),
                                ui.short(v.get("ledger_anchor") or "", 8), (v.get("created_at") or "")[:19].replace("T", " "))
                               for v in versions]))
    return "\n".join(lines)


def render_drift(report: dict) -> str:
    if not report.get("drifted"):
        return ui.ok("no drift") + ui.dim(f"  stack {report.get('stack_id')} matches its manifest")
    lines = [ui.warn(f"{len(report.get('items') or [])} resource(s) drifted")]
    for it in report.get("items") or []:
        if it.get("drift") == "missing":
            lines.append(f"  {ui.fail('-')} {it.get('kind', ''):<24} {it.get('logical_id', '')}  {ui.dim('gone from the platform')}")
        else:
            lines.append(f"  {ui.warn('~')} {it.get('kind', ''):<24} {it.get('logical_id', '')}  "
                         + ui.dim("changed: " + ", ".join(it.get("changed_keys") or [])))
            for key in it.get("changed_keys") or []:
                lines.append(ui.dim(f"      {key}: manifest {json.dumps((it.get('desired') or {}).get(key))} · "
                                    f"platform {json.dumps((it.get('observed') or {}).get(key))}"))
    if report.get("reconciled"):
        lines.append(ui.ok("reconciled") + " " + ", ".join(report["reconciled"]))
    if report.get("adopted"):
        lines.append(ui.warn("adopted") + " " + ", ".join(report["adopted"]))
    return "\n".join(lines)


# -- verify: the manifest's expectations through the platform's evaluator ----

def verify(ctx, validation: dict) -> list[dict]:
    expectations = validation.get("expectations") or []
    stack_row = validation.get("stack") or {}
    physical: dict[str, str] = {}
    if stack_row.get("stack_id"):
        physical = physical_ids(stack(ctx, stack_row["stack_id"]))
    default_ws = ctx.workspace_id or ctx.whoami().get("workspace_id")
    results: list[dict] = []
    for exp in expectations:
        name = exp.get("name") or exp.get("id") or "?"
        ws = physical.get(exp.get("workspace"), None) if exp.get("workspace") else default_ws
        if not ws:
            results.append({"name": name, "ok": False,
                            "detail": f"workspace '{exp.get('workspace')}' is not provisioned; apply first"})
            continue
        results.append(_check(ctx, name, ws, exp))
    return results


def _check(ctx, name: str, workspace_id: str, exp: dict) -> dict:
    body = {"workspace_id": workspace_id, "soul": exp.get("soul") or "reasoning",
            "labels": exp.get("labels") or {}, "fired": exp.get("fired") or []}
    try:
        out = ctx.platform.post("/v1/policies/evaluate", body)
    except PlatformError as exc:
        return {"name": name, "ok": False, "detail": str(exc)}
    resolved = out.get("resolved") or {}
    got = {lane: _level_of(resolved.get(lane)) for lane in ("enforce", "coordinate", "remediate")}
    fired = sorted({d.get("name") for d in out.get("decisions") or [] if d.get("name")})
    problems: list[str] = []
    for lane in ("enforce", "coordinate", "remediate"):
        want = exp.get(lane)
        if want is None:
            continue
        want_text = ",".join(sorted(want)) if isinstance(want, list) else want
        if want_text == "none":
            if got[lane] is not None:
                problems.append(f"{lane}: expected nothing, engine resolved {got[lane]}")
            continue
        if got[lane] != want_text:
            problems.append(f"{lane}: expected {want_text}, engine resolved {got[lane]}")
    for policy_name in exp.get("policies") or []:
        if policy_name not in fired:
            problems.append(f"policy {policy_name!r} did not fire (fired: {fired or 'none'})")
    detail = ("; ".join(problems) if problems
              else "resolved " + (", ".join(f"{k}={v}" for k, v in got.items() if v) or "nothing")
              + (f"; fired {fired}" if fired else "; nothing fired"))
    return {"name": name, "ok": not problems, "detail": detail}


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


def render_verification(results: list[dict]) -> str:
    lines = []
    for r in results:
        mark = ui.ok(" ok ") if r["ok"] else ui.fail("FAIL")
        lines.append(f" {mark} {r['name']:<36} {r['detail']}")
    passed = sum(1 for r in results if r["ok"])
    lines.append("")
    lines.append(f"Verify: {passed} passed, {len(results) - passed} failed.")
    return "\n".join(lines)


# -- env outputs: what an application needs to start ------------------------

def env_name(kind: str, logical_id: str) -> str:
    return "DMZ_" + kind.upper() + "_" + re.sub(r"[^A-Za-z0-9]+", "_", logical_id).upper().strip("_")


def write_env(ctx, detail: dict, path: Path) -> dict[str, str]:
    resources = detail.get("resources") or []
    physical = physical_ids(detail)
    workspaces = [r["physical_id"] for r in resources if r.get("kind") == "workspace" and r.get("physical_id")]
    divisions = [r["physical_id"] for r in resources if r.get("kind") == "division" and r.get("physical_id")]
    workspace = ctx.workspace_id if ctx.workspace_id in workspaces else (workspaces[0] if workspaces else ctx.workspace_id)
    division = ctx.division_id if ctx.division_id in divisions else (divisions[0] if divisions else ctx.division_id)
    values: dict[str, str] = {"DMZAGENT_BASE_URL": ctx.base_url, "DMZ_STACK_ID": detail.get("stack_id") or "",
                              "DMZ_STACK_NAME": detail.get("name") or ""}
    if workspace:
        values["DMZAGENT_WORKSPACE_ID"] = workspace
    if division:
        values["DMZAGENT_DIVISION_ID"] = division
    for r in resources:
        if r.get("kind") in ENV_KINDS and r.get("physical_id"):
            values[env_name(r["kind"], r["logical_id"])] = r["physical_id"]
    for name, value in values.items():
        set_env_var(path, name, str(value))
    return values
