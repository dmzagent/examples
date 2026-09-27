"""``dmz plan|apply|verify|destroy|outputs``: governance as code, from a file."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from giaas import render, settings
from giaas.cli import load_governance
from giaas.engine import apply as engine_apply, connect, destroy as engine_destroy, plan as engine_plan, verify as engine_verify

from .. import ui


def register(sub, formatter) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("governance", nargs="?", default="governance.py", metavar="FILE",
                        help="the governance file (default: ./governance.py)")
    common.add_argument("--var", action="append", default=[], metavar="KEY=VALUE",
                        help="a setting the file reads through giaas.settings; repeatable")
    common.add_argument("--write-env", metavar="FILE",
                        help="where outputs and minted keys are written (default: .env next to the file)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--allow-missing-requirements", action="store_true",
                        help="apply the rest even when a required Canon is not installed")
    common.add_argument("--rotate-keys", action="store_true", help="mint new application keys even if minted before")

    p = sub.add_parser("plan", parents=[common], help="what apply would change; read-only", formatter_class=formatter,
                       epilog="examples:\n  dmz plan\n  dmz plan --var posture=enforce --markdown > plan.md")
    p.add_argument("--markdown", action="store_true", help="a change set for a pull request comment")
    p.set_defaults(func=plan)

    a = sub.add_parser("apply", parents=[common], help="reconcile the workspace with the file", formatter_class=formatter,
                       epilog="examples:\n  dmz apply --write-env app/.env\n  dmz apply --var site_domain=support.example.com")
    a.set_defaults(func=apply)

    v = sub.add_parser("verify", parents=[common], help="requirements met? policies resolve as declared?",
                       formatter_class=formatter)
    v.set_defaults(func=verify)

    d = sub.add_parser("destroy", parents=[common], help="remove what the file declares", formatter_class=formatter)
    d.add_argument("--yes", action="store_true", help="do not ask")
    d.set_defaults(func=destroy)

    o = sub.add_parser("outputs", parents=[common], help="ids and outputs recorded by the last apply",
                       formatter_class=formatter)
    o.set_defaults(func=outputs)


def _prepare(args, ctx_factory):
    render.PROG = "dmz"
    for item in args.var:
        if "=" not in item:
            raise SystemExit(f"dmz: --var expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        settings[key] = value
    ctx = ctx_factory()
    gov_path = Path(args.governance).resolve()
    gov = load_governance(gov_path)
    env_file = Path(args.write_env).resolve() if args.write_env else None
    log = ctx.log or (lambda m: None)
    engine_ctx = connect(gov, gov_path, platform=ctx.platform, env_file=env_file, log=log,
                         options={"allow_missing_requirements": getattr(args, "allow_missing_requirements", False),
                                  "rotate_keys": getattr(args, "rotate_keys", False)})
    if engine_ctx.role != "tenant_admin" and args.command in ("apply", "destroy"):
        ui.hint(f"the key in use ({ctx.fingerprint()} from {ctx.key_source}) is {engine_ctx.role}; "
                f"{args.command} needs tenant_admin. Export a tenant_admin key as DMZAGENT_API_KEY.")
    return gov, engine_ctx


def plan(args, ctx_factory) -> int:
    gov, ctx = _prepare(args, ctx_factory)
    changes = engine_plan(gov, ctx)
    if args.json:
        print(render.as_json(gov, ctx, changes))
    elif args.markdown:
        print(render.markdown(gov, ctx, changes))
    else:
        print(render.text(gov, ctx, changes, verb="plan"))
        _next_step(changes)
    return 1 if any(c.action == "error" for c in changes) else 0


def apply(args, ctx_factory) -> int:
    gov, ctx = _prepare(args, ctx_factory)
    changes = engine_plan(gov, ctx)
    if not args.json:
        print(render.text(gov, ctx, changes, verb="apply"))
        print()
    engine_apply(gov, ctx, changes)
    if args.json:
        print(render.as_json(gov, ctx, changes))
    else:
        print(render.text(gov, ctx, changes, verb="applied", applied=True))
        print(f"\noutputs written to {ctx.env_file}; state in {ctx.state_path}")
        ui.hint("`dmz verify` runs the file's expectations through the platform's evaluator")
    return 0


def verify(args, ctx_factory) -> int:
    gov, ctx = _prepare(args, ctx_factory)
    results = engine_verify(gov, ctx)
    print(json.dumps(results, indent=2) if args.json else render.verification(results))
    return 0 if all(r["ok"] for r in results) else 1


def destroy(args, ctx_factory) -> int:
    gov, ctx = _prepare(args, ctx_factory)
    if not args.yes and sys.stdin.isatty():
        answer = input(f"Remove everything {gov.name} declares from {ctx.workspace_id}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    changes = engine_destroy(gov, ctx)
    print(render.as_json(gov, ctx, changes) if args.json else render.text(gov, ctx, changes, verb="destroy", applied=True))
    return 1 if any(c.action == "error" for c in changes) else 0


def outputs(args, ctx_factory) -> int:
    gov, ctx = _prepare(args, ctx_factory)
    resources = ctx.state.get("resources", {})
    if args.json:
        ui.print_json(resources)
        return 0
    if not resources:
        ui.say(ui.dim("nothing recorded yet; `dmz apply` first"))
        return 0
    rows = []
    for name, outs in sorted(resources.items()):
        for k, v in sorted((outs or {}).items()):
            rows.append((name, k, v if not isinstance(v, (dict, list)) else json.dumps(v)))
    ui.say(ui.table(["resource", "output", "value"], rows))
    ui.say()
    ui.say(ui.dim(f"env file: {ctx.env_file}    state: {ctx.state_path}"))
    return 0


def _next_step(changes) -> None:
    if any(c.action in ("create", "update", "delete") for c in changes):
        ui.hint("`dmz apply` makes these changes")
    elif any(c.action == "requires" for c in changes):
        ui.hint("a requirement is unmet; the line above says where in the console to meet it")
