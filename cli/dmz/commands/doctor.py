"""``dmz doctor``: is everything in place, and if not, what to do."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from giaas.client import ConfigError, PlatformError

from .. import ui
from ..mcp.client import McpClient, McpError

OK, WARN, FAIL = "ok", "warn", "fail"


def register(sub, formatter) -> None:
    p = sub.add_parser("doctor", help="check the key, the platform, the workspace and the tools",
                       formatter_class=formatter,
                       description="Runs the checks an example needs before it can run and says what "
                                   "to do about each one that fails. Safe to run any time; it writes nothing.")
    p.add_argument("--ollama", metavar="URL", help="also check an Ollama server (default: $OLLAMA_URL if set)")
    p.add_argument("--governance", metavar="FILE", default=None,
                   help="also load this governance file (default: ./governance.py if present)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=doctor)


def doctor(args, ctx_factory) -> int:
    results: list[dict] = []

    def add(status: str, name: str, detail: str, fix: str | None = None) -> None:
        results.append({"status": status, "check": name, "detail": detail, "fix": fix})

    # 1. Python
    v = sys.version_info
    add(OK if v >= (3, 10) else FAIL, "python", f"{v.major}.{v.minor}.{v.micro}",
        None if v >= (3, 10) else "install Python 3.10 or newer")

    # 2. Key
    ctx = ctx_factory()
    if not ctx.api_key:
        add(FAIL, "api key", "none found", "export DMZAGENT_API_KEY, pass --env-file, or run `dmz auth set`")
    elif not ctx.api_key.startswith("ck_"):
        add(FAIL, "api key", f"from {ctx.key_source} but not a ck_ key", "check what that variable or file holds")
    else:
        add(OK, "api key", f"{ctx.fingerprint()} from {ctx.key_source}")

    who = None
    if ctx.api_key and ctx.api_key.startswith("ck_"):
        # 3. Reachable and authenticated
        try:
            who = ctx.whoami()
            add(OK, "platform", f"{ctx.base_url} answers and the key authenticates")
        except ConfigError as exc:
            if "could not reach" in str(exc):
                add(FAIL, "platform", str(exc), "check --base-url / DMZAGENT_BASE_URL and the network")
            else:
                add(FAIL, "platform", str(exc), None)
        except PlatformError as exc:
            add(FAIL, "platform", f"{ctx.base_url}: {exc}",
                "the key was rejected: revoked, or minted on another deployment" if exc.status == 401 else None)

    if who:
        # 4. Role
        role = who["role"]
        if role == "tenant_admin":
            add(OK, "role", "tenant_admin: can apply governance and operate")
        elif role == "analyst":
            add(WARN, "role", "analyst: can operate and run applications; `dmz apply` needs tenant_admin",
                "export a tenant_admin key as DMZAGENT_API_KEY when applying governance")
        else:
            add(WARN, "role", f"{role}: reads only", "mint an analyst key to operate, tenant_admin to apply")
        # 5. Workspace and division
        add(OK, "workspace", who["workspace_id"])
        if who.get("division_id"):
            add(OK, "division", who["division_id"])
        else:
            add(WARN, "division", "could not be discovered",
                "set DMZAGENT_DIVISION_ID (the console shows it on the division page)")
        # 6. Installed canons via the MCP resource
        mcp = McpClient(ctx.base_url, ctx.api_key, log=lambda m: None)
        try:
            doc = mcp.read("concordia:/workspace/canons") or {}
            names = [c.get("canon_id") for c in doc.get("canons") or []]
            extra = [n for n in names if n != "cn_core_concordex"]
            if extra:
                add(OK, "canons", ", ".join(names))
            else:
                add(WARN, "canons", ", ".join(names) or "none",
                    "only the core vocabulary is installed; the examples need cn_seed_openai_agent_safety "
                    "or cn_owasp_llm_top10 (console: Library)")
        except (McpError, PlatformError, ConfigError) as exc:
            add(WARN, "canons", f"could not read the canon list: {exc}")
        # 7. MCP server
        try:
            mcp.initialize()
            tools = mcp.tools()
            resources = mcp.resources()
            add(OK, "mcp server", f"{mcp.server_info.get('name', '?')} {mcp.server_info.get('version', '')}: "
                                  f"{len(tools)} tools, {len(resources)} resources, protocol {mcp.protocol_version}")
            if mcp.protocol_version != "2025-06-18":
                add(WARN, "mcp hosts", f"the server speaks protocol {mcp.protocol_version}, which Claude Code, "
                    "Claude Desktop and Cursor do not accept directly",
                    "use `dmz mcp bridge` (see `dmz mcp config --client ...`)")
        except (McpError, PlatformError, ConfigError) as exc:
            add(FAIL, "mcp server", str(exc), "the key may lack the mcp scope; mint one without scope limits")

    # 8. Ollama, when asked or configured
    ollama = args.ollama or os.environ.get("OLLAMA_URL") or ctx.env.get("OLLAMA_URL")
    if ollama:
        try:
            with urllib.request.urlopen(ollama.rstrip("/") + "/api/tags", timeout=5) as resp:
                models = [m.get("name") for m in json.loads(resp.read()).get("models", [])]
            add(OK if models else WARN, "ollama", f"{ollama}: {', '.join(models) or 'no models pulled'}",
                None if models else "pull a tool-capable model, e.g. `ollama pull llama3.1`")
        except (urllib.error.URLError, ValueError, OSError) as exc:
            add(FAIL, "ollama", f"{ollama}: {exc}", "start Ollama (`ollama serve`) or set OLLAMA_URL")

    # 9. A governance file
    gov_path = Path(args.governance) if args.governance else Path("governance.py")
    if gov_path.exists():
        try:
            from giaas.cli import load_governance
            gov = load_governance(gov_path.resolve())
            kinds = {}
            for r in gov.resources:
                kinds[r.kind] = kinds.get(r.kind, 0) + 1
            add(OK, "governance", f"{gov_path}: {gov.name}, " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
        except Exception as exc:  # a broken file is the finding
            add(FAIL, "governance", f"{gov_path} does not load: {exc}")

    if args.json:
        ui.print_json(results)
    else:
        for r in results:
            mark = {OK: ui.ok("✓"), WARN: ui.warn("!"), FAIL: ui.fail("✗")}[r["status"]]
            ui.say(f"  {mark} {ui.bold(r['check'].ljust(11))} {r['detail']}")
            if r.get("fix"):
                ui.say(f"    {ui.dim('→ ' + r['fix'])}")
        bad = [r for r in results if r["status"] == FAIL]
        warned = [r for r in results if r["status"] == WARN]
        ui.say()
        if bad:
            ui.say(ui.fail(f"{len(bad)} problem(s)") + (f", {len(warned)} warning(s)" if warned else ""))
        elif warned:
            ui.say(ui.warn(f"ready, with {len(warned)} warning(s)"))
        else:
            ui.say(ui.ok("ready"))
    return 1 if any(r["status"] == FAIL for r in results) else 0
