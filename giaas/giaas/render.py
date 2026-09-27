"""Rendering a change set for a terminal, a pull request, or a machine."""
from __future__ import annotations

import json
from typing import Any

from .engine import Change, Ctx
from .resources import Governance


def header(gov: Governance, ctx: Ctx, verb: str) -> str:
    from .client import fingerprint
    return (f"giaas {verb} · {gov.name}\n"
            f"workspace {ctx.workspace_id} · division {ctx.division_id} · "
            f"role {ctx.role} · key {fingerprint(ctx.platform.api_key)} · {ctx.platform.base_url}")


def summarize(changes: list[Change]) -> str:
    counts: dict[str, int] = {}
    for c in changes:
        counts[c.action] = counts.get(c.action, 0) + 1
    order = ("create", "update", "delete", "noop", "requires", "skip", "error")
    words = {"create": "to add", "update": "to change", "delete": "to remove", "noop": "unchanged",
             "requires": "requirement", "skip": "skipped", "error": "failed"}
    parts = [f"{counts[a]} {words[a]}" for a in order if counts.get(a)]
    return "Plan: " + (", ".join(parts) if parts else "nothing declared") + "."


def text(gov: Governance, ctx: Ctx, changes: list[Change], *, verb: str, applied: bool = False) -> str:
    lines = [header(gov, ctx, verb), ""]
    width = max((len(c.resource.kind) for c in changes), default=10)
    for c in changes:
        name = c.resource.name
        status = c.summary
        if applied and c.result:
            shown = {k: v for k, v in c.result.items() if k in ("cb_policy_id", "policy_id", "embed_id",
                     "logic_canon_id", "version", "prefix", "installed")}
            if shown:
                status += " → " + ", ".join(f"{k}={v}" for k, v in shown.items())
        if c.error:
            status += f" [error: {c.error}]"
        lines.append(f" {c.symbol} {c.resource.kind:<{width}}  {name!s:<34} {status}")
    lines.append("")
    lines.append(summarize(changes))
    return "\n".join(lines)


def markdown(gov: Governance, ctx: Ctx, changes: list[Change]) -> str:
    lines = [f"### giaas plan · `{gov.name}`",
             f"workspace `{ctx.workspace_id}` · division `{ctx.division_id}` · base `{ctx.platform.base_url}`",
             "", "| | kind | name | change |", "|---|---|---|---|"]
    for c in changes:
        lines.append(f"| `{c.symbol}` | {c.resource.kind} | {c.resource.name} | {c.summary} |")
    lines += ["", summarize(changes)]
    return "\n".join(lines)


def as_json(gov: Governance, ctx: Ctx, changes: list[Change]) -> str:
    doc: dict[str, Any] = {
        "governance": gov.name, "workspace_id": ctx.workspace_id, "division_id": ctx.division_id,
        "base_url": ctx.platform.base_url, "summary": summarize(changes),
        "changes": [{"action": c.action, "kind": c.resource.kind, "name": c.resource.name,
                     "summary": c.summary, "result": c.result, "error": c.error} for c in changes],
    }
    return json.dumps(doc, indent=2, default=str)


def verification(results: list[dict]) -> str:
    lines = []
    for r in results:
        mark = "ok " if r["ok"] else "FAIL"
        lines.append(f" {mark} {r['kind']:<12} {r['name']!s:<34} {r['detail']}")
    failed = sum(1 for r in results if not r["ok"])
    lines.append("")
    lines.append(f"Verify: {len(results) - failed} passed, {failed} failed.")
    return "\n".join(lines)
