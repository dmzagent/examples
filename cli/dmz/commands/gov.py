"""The Solution Manifest wheel: ``dmz validate | plan | apply | verify |
destroy | drift | stack | stacks``. The platform validates, plans and applies;
these commands submit the file and show the answer."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .. import manifest as MF
from .. import ui
from ..client import ConfigError


def register(sub, formatter) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("manifest", nargs="?", default=None, metavar="FILE",
                        help="the Solution Manifest (default: ./solution.yaml)")
    common.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                        help="fill ${NAME} in the file; repeatable")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    v = sub.add_parser("validate", parents=[common], help="schema and semantic check, plus the guardrail preview",
                       formatter_class=formatter,
                       epilog="exit 0 valid · 1 invalid (or a guardrail would refuse it, with --strict)")
    v.add_argument("--strict", action="store_true", help="also fail on guardrail violations")
    v.set_defaults(func=validate)

    p = sub.add_parser("plan", parents=[common], help="the Change Set against the deployed Stack; read-only",
                       formatter_class=formatter,
                       epilog="examples:\n  dmz plan\n  dmz plan --var posture=observe\n  dmz plan --markdown > plan.md   # a PR comment")
    p.add_argument("--markdown", action="store_true", help="a change set for a pull request comment")
    p.add_argument("--all", action="store_true", help="show unchanged resources too")
    p.add_argument("--strict", action="store_true", help="exit 1 when a guardrail would refuse the apply")
    p.set_defaults(func=plan)

    a = sub.add_parser("apply", parents=[common], help="reconcile the Stack with the file (maker-checker enforced)",
                       formatter_class=formatter,
                       epilog="examples:\n  dmz apply --approved-by ravi@harbor.example --write-env app/.env\n"
                              "  dmz apply --applied-by \"$PR_AUTHOR\" --approved-by \"$PR_MERGER\"   # in CI, on merge\n\n"
                              "The platform requires an approver distinct from the applier (four-eyes). In CI that is\n"
                              "the merger; locally name the reviewer, or set DMZ_APPROVED_BY.")
    a.add_argument("--applied-by", default=None, help="the maker (default: your principal)")
    a.add_argument("--approved-by", default=None, help="the checker (default: $DMZ_APPROVED_BY)")
    a.add_argument("--write-env", metavar="FILE", help="write the ids an application needs to this env file")
    a.add_argument("--yes", action="store_true", help="do not show the plan and ask first")
    a.set_defaults(func=apply)

    vf = sub.add_parser("verify", parents=[common], help="run the file's expectations on the platform's evaluator",
                        formatter_class=formatter)
    vf.set_defaults(func=verify)

    d = sub.add_parser("destroy", parents=[common], help="remove what the Stack manages; topology is retained",
                       formatter_class=formatter)
    d.add_argument("--applied-by", default=None)
    d.add_argument("--approved-by", default=None, help="the checker (default: $DMZ_APPROVED_BY)")
    d.add_argument("--yes", action="store_true", help="do not ask")
    d.set_defaults(func=destroy)

    dr = sub.add_parser("drift", parents=[common], help="has anything managed changed on the platform?",
                        formatter_class=formatter,
                        epilog="examples:\n  dmz drift                 # report\n  dmz drift --reconcile     # put the manifest back\n"
                               "  dmz drift --adopt         # accept the platform's state as the new baseline")
    dr.add_argument("--stack", default=None, help="the stack name (default: the manifest's)")
    dr.add_argument("--reconcile", action="store_true")
    dr.add_argument("--adopt", action="store_true")
    dr.set_defaults(func=drift)

    st = sub.add_parser("stack", parents=[common], help="a Stack: its resources, physical ids and versions",
                        formatter_class=formatter)
    st.add_argument("--stack", default=None, help="the stack name (default: the manifest's)")
    st.set_defaults(func=stack)

    ss = sub.add_parser("stacks", help="the vendor's Stacks", formatter_class=formatter)
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=stacks)


# -- helpers -----------------------------------------------------------------

def _variables(args) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in args.var:
        if "=" not in item:
            raise ConfigError(f"--var expects NAME=VALUE, got {item!r}")
        name, value = item.split("=", 1)
        out[name] = value
    return out


def _load(args, ctx) -> tuple[Path, str]:
    path = MF.find_file(args.manifest)
    text = MF.load(ctx, path, _variables(args))
    left = MF.unresolved(text)
    if left:
        ui.hint("unfilled placeholders: " + ", ".join(f"${{{n}}}" for n in left)
                + " (they are filled from the key's context when it is bound to a workspace)")
    return path, text


def _approver(args) -> str | None:
    return args.approved_by or os.environ.get("DMZ_APPROVED_BY") or None


def _stack_from(args, ctx, validation: dict | None = None) -> dict:
    name = getattr(args, "stack", None)
    if not name:
        if validation is None:
            _, text = _load(args, ctx)
            validation = MF.validate(ctx, text)
        name = (validation.get("metadata") or {}).get("name")
        row = validation.get("stack")
        if row:
            return row
    row = MF.stack_by_name(ctx, name or "")
    if not row:
        raise ConfigError(f"no stack named {name!r} in this vendor; `dmz apply` creates it")
    return row


# -- commands ----------------------------------------------------------------

def validate(args, ctx_factory) -> int:
    ctx = ctx_factory()
    _, text = _load(args, ctx)
    result = MF.validate(ctx, text)
    if args.json:
        ui.print_json(result)
    else:
        ui.say(MF.render_validation(result))
    if not result.get("ok"):
        return 1
    return 1 if (args.strict and result.get("guardrails")) else 0


def plan(args, ctx_factory) -> int:
    ctx = ctx_factory()
    path, text = _load(args, ctx)
    result = MF.plan(ctx, text)
    if args.json:
        ui.print_json(result)
    elif args.markdown:
        print(MF.render_plan_markdown(result, path.stem))
    else:
        ui.say(MF.render_plan(result, show_unchanged=args.all))
        if any(c.get("action") not in ("no_change",) for c in result.get("changes") or []):
            ui.hint("`dmz apply --approved-by <reviewer>` makes these changes")
    return 1 if (args.strict and result.get("guardrails")) else 0


def apply(args, ctx_factory) -> int:
    ctx = ctx_factory()
    _, text = _load(args, ctx)
    approver = _approver(args)
    if not args.json and not args.yes:
        preview = MF.plan(ctx, text)
        ui.say(MF.render_plan(preview))
        ui.say()
        actionable = sum(preview.get("summary", {}).get(k, 0) for k in ("add", "update", "replace", "delete"))
        if not actionable:
            ui.say(ui.ok("no change") + ui.dim("  nothing to apply"))
            if args.write_env and preview.get("exists"):
                _write_env(ctx, preview["stack_id"], Path(args.write_env))
            return 0
        if sys.stdin.isatty():
            answer = input("Apply? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                ui.say("aborted")
                return 1
    if not approver:
        ui.hint("no approver named; the platform's maker-checker floor will refuse unless the vendor's "
                "guardrails allow it (pass --approved-by or set DMZ_APPROVED_BY)")
    result = MF.apply(ctx, text, applied_by=args.applied_by, approved_by=approver)
    if args.json:
        ui.print_json(result)
    else:
        ui.say(MF.render_apply(result))
    if args.write_env and result.get("stack_id"):
        values = _write_env(ctx, result["stack_id"], Path(args.write_env))
        if not args.json:
            ui.say(ui.dim(f"  {len(values)} values written to {args.write_env}"))
    if not args.json:
        ui.hint("`dmz verify` runs the file's expectations; `dmz stack` shows the ids; `dmz drift` watches for changes")
    return 0


def _write_env(ctx, stack_id: str, path: Path) -> dict:
    detail = MF.stack(ctx, stack_id)
    return MF.write_env(ctx, detail, path)


def verify(args, ctx_factory) -> int:
    ctx = ctx_factory()
    _, text = _load(args, ctx)
    validation = MF.validate(ctx, text)
    if not validation.get("ok"):
        ui.say(MF.render_validation(validation))
        return 1
    results = MF.verify(ctx, validation)
    if args.json:
        ui.print_json(results)
    else:
        if not results:
            ui.say(ui.dim("the manifest declares no expectations"))
            return 0
        ui.say(MF.render_verification(results))
    return 0 if all(r["ok"] for r in results) else 1


def destroy(args, ctx_factory) -> int:
    ctx = ctx_factory()
    row = _stack_from(args, ctx)
    if not args.yes and sys.stdin.isatty():
        answer = input(f"Remove everything stack {row.get('name')!r} manages? Divisions, workspaces and Logic Canons "
                       f"are retained. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            ui.say("aborted")
            return 1
    result = MF.destroy(ctx, row["stack_id"], applied_by=args.applied_by, approved_by=_approver(args))
    if args.json:
        ui.print_json(result)
    else:
        ui.say(MF.render_apply(result))
        if result.get("retained"):
            ui.say(ui.dim("  retained: " + ", ".join(result["retained"])))
    return 0


def drift(args, ctx_factory) -> int:
    ctx = ctx_factory()
    row = _stack_from(args, ctx)
    report = MF.drift(ctx, row["stack_id"], reconcile=args.reconcile, adopt=args.adopt)
    if args.json:
        ui.print_json(report)
    else:
        ui.say(MF.render_drift(report))
        if report.get("drifted") and not (args.reconcile or args.adopt):
            ui.hint("`dmz drift --reconcile` puts the manifest back; `--adopt` accepts what is there")
    return 1 if report.get("drifted") and not (args.reconcile or args.adopt) else 0


def stack(args, ctx_factory) -> int:
    ctx = ctx_factory()
    row = _stack_from(args, ctx)
    detail = MF.stack(ctx, row["stack_id"])
    if args.json:
        ui.print_json(detail)
    else:
        ui.say(MF.render_stack(detail))
    return 0


def stacks(args, ctx_factory) -> int:
    ctx = ctx_factory()
    rows = MF.stacks(ctx)
    if args.json:
        ui.print_json(rows)
        return 0
    ui.say(ui.table(["stack", "status", "version", "updated"],
                    [(r.get("name"), r.get("status"), r.get("current_version"), (r.get("updated_at") or "")[:19].replace("T", " "))
                     for r in rows]))
    return 0
