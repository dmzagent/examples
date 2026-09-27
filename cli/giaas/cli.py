"""The command line: giaas plan | apply | verify | destroy | outputs <governance.py>."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from . import settings
from .client import ConfigError, PlatformError
from .engine import RequirementUnmet, apply, connect, destroy, plan, verify
from .resources import DeclarationError, Governance
from . import render


def load_governance(path: Path) -> Governance:
    """Import the governance file and return its `governance` object (or the
    result of its `build()` function)."""
    if not path.exists():
        raise ConfigError(f"no such governance file: {path}")
    spec = importlib.util.spec_from_file_location(f"governance_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    gov = getattr(module, "governance", None)
    if gov is None and callable(getattr(module, "build", None)):
        gov = module.build()
    if not isinstance(gov, Governance):
        raise ConfigError(f"{path} must define `governance = Governance(...)` or `build()`")
    return gov


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="giaas",
        description="Governance Infrastructure as a Service — declare DMZAgent governance in code, "
                    "then plan, apply, verify and destroy it against a workspace.")
    parser.add_argument("command", choices=["plan", "apply", "verify", "destroy", "outputs"])
    parser.add_argument("governance", help="path to the governance file (Python)")
    parser.add_argument("--var", action="append", default=[], metavar="KEY=VALUE",
                        help="a setting the governance file can read via giaas.settings")
    parser.add_argument("--env-file", default=None,
                        help="dotenv file to write outputs and minted keys to (default: .env next to the file)")
    parser.add_argument("--allow-missing-requirements", action="store_true",
                        help="apply the rest even when a required Canon is not installed")
    parser.add_argument("--rotate-keys", action="store_true", help="mint new SDK keys even if minted before")
    parser.add_argument("--yes", action="store_true", help="destroy without asking")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--markdown", action="store_true", help="a change set for a pull request comment")
    parser.add_argument("--quiet", action="store_true", help="no progress lines")
    args = parser.parse_args(argv)

    for item in args.var:
        if "=" not in item:
            parser.error(f"--var expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        settings[key] = value

    log = (lambda msg: None) if (args.quiet or args.json) else (lambda msg: print(f"  … {msg}", file=sys.stderr))
    gov_path = Path(args.governance).resolve()
    try:
        gov = load_governance(gov_path)
        env_file = Path(args.env_file).resolve() if args.env_file else None
        ctx = connect(gov, gov_path, env_file=env_file, log=log,
                      options={"allow_missing_requirements": args.allow_missing_requirements,
                               "rotate_keys": args.rotate_keys})
        if args.command == "plan":
            changes = plan(gov, ctx)
            print(_render(gov, ctx, changes, args, verb="plan"))
            return 0 if not any(c.action == "error" for c in changes) else 1
        if args.command == "apply":
            changes = plan(gov, ctx)
            if not args.json:
                print(render.text(gov, ctx, changes, verb="apply"))
                print()
            apply(gov, ctx, changes)
            if args.json:
                print(render.as_json(gov, ctx, changes))
            else:
                print(render.text(gov, ctx, changes, verb="applied", applied=True))
                print(f"\noutputs written to {ctx.env_file}; state in {ctx.state_path}")
            return 0
        if args.command == "verify":
            results = verify(gov, ctx)
            print(json.dumps(results, indent=2) if args.json else render.verification(results))
            return 0 if all(r["ok"] for r in results) else 1
        if args.command == "destroy":
            if not args.yes and sys.stdin.isatty():
                answer = input(f"Remove everything {gov.name} declares from {ctx.workspace_id}? [y/N] ")
                if answer.strip().lower() not in ("y", "yes"):
                    print("aborted")
                    return 1
            changes = destroy(gov, ctx)
            print(render.as_json(gov, ctx, changes) if args.json else render.text(gov, ctx, changes, verb="destroy", applied=True))
            return 0 if not any(c.action == "error" for c in changes) else 1
        if args.command == "outputs":
            print(json.dumps(ctx.state.get("resources", {}), indent=2, sort_keys=True))
            return 0
    except (ConfigError, DeclarationError, RequirementUnmet) as exc:
        print(f"giaas: {exc}", file=sys.stderr)
        return 2
    except PlatformError as exc:
        print(f"giaas: the platform refused a request — {exc}", file=sys.stderr)
        return 3
    return 0


def _render(gov, ctx, changes, args, *, verb: str) -> str:
    if args.json:
        return render.as_json(gov, ctx, changes)
    if args.markdown:
        return render.markdown(gov, ctx, changes)
    return render.text(gov, ctx, changes, verb=verb)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
