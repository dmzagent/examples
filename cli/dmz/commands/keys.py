"""``dmz keys mint``: an application key for a workspace the Stack manages."""
from __future__ import annotations

from pathlib import Path

from .. import manifest as MF
from .. import ui
from ..client import ConfigError, fingerprint
from ..envfile import set_env_var


def register(sub, formatter) -> None:
    p = sub.add_parser("keys", help="mint application keys", formatter_class=formatter)
    s = p.add_subparsers(dest="keys_command", metavar="<action>", required=True)
    m = s.add_parser("mint", help="mint a least-privilege key for a workspace and write it to an env file",
                     formatter_class=formatter,
                     epilog="examples:\n  dmz keys mint --workspace support --label \"support site\" --write-env app/.env\n"
                            "  dmz keys mint --workspace ws_5056ea22fe --label runner --env-var RUNNER_KEY --write-env .env\n\n"
                            "The secret is written to the env file (mode 0600) and never printed unless --show is passed.")
    m.add_argument("manifest", nargs="?", default=None, metavar="FILE", help="the manifest whose stack names the workspace")
    m.add_argument("--workspace", required=True, help="a workspace logical id from the manifest, or a workspace id")
    m.add_argument("--label", required=True, help="what the key is for; shown in the console")
    m.add_argument("--env-var", default="DMZAGENT_APP_KEY", help="the variable name (default: DMZAGENT_APP_KEY)")
    m.add_argument("--write-env", metavar="FILE", default=None, help="the env file to write (default: ./app/.env)")
    m.add_argument("--stack", default=None, help="the stack name (default: the manifest's)")
    m.add_argument("--show", action="store_true", help="print the secret instead of writing it")
    m.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=mint)


def mint(args, ctx_factory) -> int:
    ctx = ctx_factory()
    workspace_id = args.workspace
    if not workspace_id.startswith("ws_"):
        name = args.stack
        if not name:
            from .gov import _load
            _, text = _load(args, ctx)
            validation = MF.validate(ctx, text)
            row = validation.get("stack")
            name = (validation.get("metadata") or {}).get("name")
        else:
            row = MF.stack_by_name(ctx, name)
        if not row:
            raise ConfigError(f"no stack named {name!r}; `dmz apply` first, then mint keys for its workspaces")
        physical = MF.physical_ids(MF.stack(ctx, row["stack_id"]))
        if args.workspace not in physical:
            raise ConfigError(f"the stack has no workspace '{args.workspace}' (known: {', '.join(sorted(physical)) or 'none'})")
        workspace_id = physical[args.workspace]
    out = ctx.platform.post("/v1/agent-stream/api-keys", {"workspace_id": workspace_id, "label": args.label})
    secret = out.get("key") or out.get("secret") or ""
    if not secret:
        raise ConfigError("the platform did not return a key")
    if args.json:
        ui.print_json({"workspace_id": workspace_id, "prefix": fingerprint(secret), "env_var": args.env_var,
                       **({"key": secret} if args.show else {})})
        return 0
    if args.show:
        print(secret)
        return 0
    path = Path(args.write_env or "app/.env")
    set_env_var(path, args.env_var, secret)
    set_env_var(path, "DMZAGENT_WORKSPACE_ID", workspace_id)
    set_env_var(path, "DMZAGENT_BASE_URL", ctx.base_url)
    ui.say(ui.ok("minted"), f"{fingerprint(secret)} for {workspace_id}", ui.dim(f"→ {args.env_var} in {path}"))
    ui.hint("the secret is recoverable only from that file; revoke it in the console if it leaks")
    return 0
