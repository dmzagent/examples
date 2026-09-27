"""``dmz mcp ...``: the platform's MCP server from the terminal, and in front
of any MCP host through the stdio bridge."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .. import __version__, ui
from ..mcp.bridge import Bridge, LATEST_VERSION
from ..mcp.client import McpClient, McpError, READ_ONLY_TOOLS

CLIENTS = ("claude-code", "claude-desktop", "cursor", "windsurf", "vscode", "generic")


def register(sub, formatter) -> None:
    p = sub.add_parser("mcp", help="the platform's MCP server: tools, resources, calls, host config, bridge",
                       formatter_class=formatter,
                       epilog="examples:\n  dmz mcp tools\n  dmz mcp enforce customer:alice issue_refund --payload '{\"amount\": 50}'\n"
                              "  dmz mcp read concordia:/workspace/policies\n"
                              "  dmz mcp config --client claude-code\n  dmz mcp bridge   # what a host launches")
    s = p.add_subparsers(dest="mcp_command", metavar="<action>", required=True)

    t = s.add_parser("tools", help="list the tools the server offers", formatter_class=formatter)
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=tools)

    r = s.add_parser("resources", help="list the resources the server offers", formatter_class=formatter)
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=resources)

    rd = s.add_parser("read", help="read a resource (JSON)", formatter_class=formatter,
                      epilog="examples:\n  dmz mcp read concordia:/workspace/canons\n"
                             "  dmz mcp read concordia:/workspace/recent-ledger --since 0 --limit 20")
    rd.add_argument("uri")
    rd.add_argument("--since", type=int, default=None)
    rd.add_argument("--limit", type=int, default=None)
    rd.set_defaults(func=read)

    c = s.add_parser("call", help="call any tool with arguments", formatter_class=formatter,
                     epilog="examples:\n  dmz mcp call query_corpus --arg query=refund --arg limit=5\n"
                            "  dmz mcp call get_subject_soul --arg subject_id=customer:alice\n"
                            "  dmz mcp call enforce_covenant --args '{\"subject_id\": \"customer:alice\", \"action_kind\": \"issue_refund\"}'\n"
                            "\n--arg values that parse as JSON (numbers, true, objects) are passed as such; others as strings.")
    c.add_argument("tool")
    c.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE")
    c.add_argument("--args", default=None, metavar="JSON", help="all arguments as one JSON object")
    c.add_argument("--raw", action="store_true", help="the server's full result envelope")
    c.set_defaults(func=call)

    e = s.add_parser("enforce", help="pre-flight: may this action happen? (exit 0 allow, 1 review, 3 block)",
                     formatter_class=formatter,
                     epilog="example:\n  dmz mcp enforce customer:alice issue_refund --payload '{\"amount\": 50}' && ./refund.sh")
    e.add_argument("subject")
    e.add_argument("action_kind")
    e.add_argument("--payload", default=None, metavar="JSON")
    e.add_argument("--context", default=None, metavar="JSON")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=enforce)

    rc = s.add_parser("record", help="post-hoc: put a decision on the ledger", formatter_class=formatter,
                      epilog="example:\n  dmz mcp record customer:alice refund_issued --payload '{\"amount\": 50}' --actor human")
    rc.add_argument("subject")
    rc.add_argument("decision_kind")
    rc.add_argument("--payload", default=None, metavar="JSON")
    rc.add_argument("--actor", default="agent", choices=["agent", "human", "system"])
    rc.add_argument("--outcome", default="completed", choices=["completed", "aborted", "rejected"])
    rc.add_argument("--json", action="store_true")
    rc.set_defaults(func=record)

    pg = s.add_parser("ping", help="handshake with the server and report who it thinks you are",
                      formatter_class=formatter)
    pg.add_argument("--json", action="store_true")
    pg.set_defaults(func=ping)

    cf = s.add_parser("config", help="the configuration that attaches the server to an MCP host",
                      formatter_class=formatter,
                      epilog="examples:\n  dmz mcp config --client claude-code\n"
                             "  dmz mcp config --client claude-desktop --env-file app/.env\n"
                             "  dmz mcp config --client cursor > .cursor/mcp.json")
    cf.add_argument("--client", choices=CLIENTS, default="claude-code")
    cf.add_argument("--name", default="dmzagent", help="the server name in the host (default: dmzagent)")
    cf.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                    help="stdio through the bridge (works today) or direct http (needs a standard-MCP server)")
    cf.set_defaults(func=config)

    b = s.add_parser("bridge", help="run the stdio bridge (what an MCP host launches)", formatter_class=formatter,
                     description="Speaks standard MCP on stdin/stdout and forwards to the platform with the key "
                                 "resolved the usual way (environment, --env-file, saved profile). Logs go to stderr.")
    b.add_argument("--log-level", default="warning", choices=["debug", "info", "warning", "error"])
    b.add_argument("--name", default="dmzagent", help="the server name reported to the host")
    b.set_defaults(func=bridge)


# -- helpers -----------------------------------------------------------------

def _client(ctx) -> McpClient:
    return McpClient(ctx.base_url, ctx.require_key(), log=ctx.log or (lambda m: None), client_version=__version__)


def _json_arg(text: str | None, what: str) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"dmz: {what} must be JSON: {exc}")
    if not isinstance(value, dict):
        raise SystemExit(f"dmz: {what} must be a JSON object")
    return value


def _subject(ctx, text: str) -> str:
    # The server canonicalizes logical ids itself; pass canonical ones through.
    return text


# -- commands ----------------------------------------------------------------

def tools(args, ctx_factory) -> int:
    client = _client(ctx_factory())
    items = client.tools()
    if args.json:
        ui.print_json(items)
        return 0
    rows = []
    for t in items:
        access = ui.dim("read") if t.get("name") in READ_ONLY_TOOLS else ui.warn("write")
        required = ", ".join((t.get("inputSchema") or {}).get("required") or [])
        rows.append((ui.bold(t.get("name", "")), access, required or ui.dim("—"), ui.short(t.get("description", ""), 70)))
    ui.say(ui.table(["tool", "access", "required arguments", "what it does"], rows))
    ui.say()
    ui.hint("`dmz mcp call <tool> --arg k=v` calls one; `dmz mcp enforce` and `dmz mcp record` are the two every agent makes")
    return 0


def resources(args, ctx_factory) -> int:
    client = _client(ctx_factory())
    items = client.resources()
    if args.json:
        ui.print_json(items)
        return 0
    ui.say(ui.table(["uri", "name", "description"],
                    [(ui.bold(r.get("uri", "")), r.get("name", ""), ui.short(r.get("description", ""), 70)) for r in items]))
    ui.say()
    ui.hint("`dmz mcp read <uri>` fetches one")
    return 0


def read(args, ctx_factory) -> int:
    client = _client(ctx_factory())
    uri = args.uri
    params = {k: v for k, v in (("since", args.since), ("limit", args.limit)) if v is not None}
    if params:
        uri += ("&" if "?" in uri else "?") + "&".join(f"{k}={v}" for k, v in params.items())
    ui.print_json(client.read(uri))
    return 0


def call(args, ctx_factory) -> int:
    client = _client(ctx_factory())
    arguments = _json_arg(args.args, "--args")
    for item in args.arg:
        if "=" not in item:
            raise SystemExit(f"dmz: --arg expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        try:
            arguments[key] = json.loads(value)
        except ValueError:
            arguments[key] = value
    result = client.call_raw(args.tool, arguments) if args.raw else client.call(args.tool, arguments)
    ui.print_json(result)
    return 0


def enforce(args, ctx_factory) -> int:
    client = _client(ctx_factory())
    result = client.enforce(args.subject, args.action_kind, _json_arg(args.payload, "--payload"),
                            _json_arg(args.context, "--context"))
    verdict = result.get("verdict")
    if args.json:
        ui.print_json(result)
    else:
        change = result.get("cb_state_change") or {}
        ui.say(ui.kv([
            ("verdict", ui.verdict(verdict)),
            ("breaker", ui.breaker(change.get("state"), bool(change.get("warning")))),
            ("rationale", result.get("rationale") or ""),
            ("policies", ", ".join(result.get("policy_ids") or []) or ui.dim("none")),
            ("ledger", result.get("ledger_entry_id") or ""),
        ]))
    return {"allow": 0, "review": 1}.get(verdict, 3)


def record(args, ctx_factory) -> int:
    client = _client(ctx_factory())
    result = client.record(args.subject, args.decision_kind, _json_arg(args.payload, "--payload"),
                           actor=args.actor, outcome=args.outcome)
    if args.json:
        ui.print_json(result)
    else:
        ui.say(ui.ok("recorded"), args.decision_kind, ui.dim("for"), args.subject)
        ui.say(ui.kv([("ledger", result.get("ledger_entry_id") or ""), ("index", result.get("index")),
                      ("chain head", ui.short(result.get("chain_head_hash") or "", 16))]))
    return 0


def ping(args, ctx_factory) -> int:
    client = _client(ctx_factory())
    info = client.initialize()
    alive = client.ping()
    if args.json:
        ui.print_json({**info, "ping": alive})
        return 0
    server = info.get("serverInfo") or {}
    hint = info.get("principalHint") or {}
    ui.say(ui.kv([
        ("server", f"{server.get('name', '?')} {server.get('version', '')}".strip()),
        ("protocol", info.get("protocolVersion") or "?"),
        ("workspace", hint.get("workspace_id") or "?"),
        ("role", hint.get("role") or "?"),
        ("ping", ui.ok("ok") if alive else ui.fail("no answer")),
    ]))
    if info.get("protocolVersion") != LATEST_VERSION:
        ui.hint("MCP hosts expect a dated protocol version; `dmz mcp bridge` presents one in front of this server")
    return 0 if alive else 1


def config(args, ctx_factory) -> int:
    ctx = ctx_factory()
    name = args.name
    if args.transport == "http":
        url = ctx.base_url + "/mcp/v1"
        ui.note("direct HTTP needs the server to speak standard MCP; today it answers protocol 1.0, "
                "so hosts refuse the handshake. Keep this for when that changes; use stdio meanwhile.")
        if args.client == "claude-code":
            print(f'claude mcp add --transport http {name} {url} --header "Authorization: Bearer $DMZAGENT_API_KEY"')
        else:
            print(json.dumps({"mcpServers": {name: {"type": "http", "url": url,
                                                    "headers": {"Authorization": "Bearer ${DMZAGENT_API_KEY}"}}}}, indent=2))
        return 0

    command, base_args, env = _launcher()
    bridge_args = list(base_args)
    if ctx.base_url != "https://api.dmzagent.com":
        bridge_args += ["--base-url", ctx.base_url]
    bridge_args += ["mcp", "bridge"]
    if ctx.env_file:
        bridge_args += ["--env-file", str(ctx.env_file)]
        source = f"the key in {ctx.env_file}"
    elif ctx.key_source.startswith("profile"):
        bridge_args += ["--profile", ctx.profile]
        source = f"the saved profile {ctx.profile!r}"
    elif ctx.api_key:
        source = "$DMZAGENT_API_KEY, which the host must be able to see (or run `dmz auth set` and use the profile)"
    else:
        source = "no key found yet: run `dmz auth set` first"
    ui.note(f"server {name!r} → {command} {' '.join(bridge_args)}; the bridge uses {source}")

    if args.client == "claude-code":
        env_flags = " ".join(f"-e {k}={v}" for k, v in env.items())
        print(f"claude mcp add {name} -s user {env_flags} -- {command} {' '.join(bridge_args)}".replace("  ", " "))
        ui.hint("then `claude mcp list` shows it; in a session, the tools appear as mcp__dmzagent__*")
        return 0
    server = {"command": command, "args": bridge_args}
    if env:
        server["env"] = env
    if args.client == "vscode":
        print(json.dumps({"servers": {name: {"type": "stdio", **server}}}, indent=2))
        ui.hint("goes in .vscode/mcp.json (workspace) or the user settings' mcp.servers")
        return 0
    print(json.dumps({"mcpServers": {name: server}}, indent=2))
    where = {
        "claude-desktop": "claude_desktop_config.json (macOS: ~/Library/Application Support/Claude/, "
                          "Windows: %APPDATA%\\Claude\\), then restart Claude Desktop",
        "cursor": "~/.cursor/mcp.json (or .cursor/mcp.json in the project)",
        "windsurf": "~/.codeium/windsurf/mcp_config.json",
        "generic": "wherever your host reads mcpServers from",
    }[args.client]
    ui.hint(f"goes in {where}")
    return 0


def _launcher() -> tuple[str, list[str], dict]:
    """How a host should start the bridge on this machine: the installed `dmz`
    when there is one, else this interpreter with the toolkit on PYTHONPATH."""
    found = shutil.which("dmz")
    if found:
        return found, [], {}
    cli_dir = Path(__file__).resolve().parents[2]
    return sys.executable or "python3", ["-m", "dmz"], {"PYTHONPATH": str(cli_dir)}


def bridge(args, ctx_factory) -> int:
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, args.log_level.upper()),
                        format="dmz mcp bridge: %(message)s")
    ctx = ctx_factory()
    client = McpClient(ctx.base_url, ctx.require_key(), log=logging.getLogger("dmz.mcp.client").info,
                       client_name="dmz-bridge", client_version=__version__)
    logging.getLogger("dmz.mcp.bridge").info("bridging stdio to %s (key %s from %s)", ctx.base_url,
                                             ctx.fingerprint(), ctx.key_source)
    return Bridge(client, name=args.name, version=__version__).serve()
