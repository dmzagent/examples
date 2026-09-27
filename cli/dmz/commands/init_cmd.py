"""``dmz init``: a Solution Manifest to start from."""
from __future__ import annotations

from pathlib import Path

from .. import ui
from ..templates import TEMPLATES, render


def register(sub, formatter) -> None:
    p = sub.add_parser("init", help="write a solution.yaml to start from", formatter_class=formatter,
                       epilog="templates:\n" + "\n".join(f"  {name:<9} {summary}" for name, summary in
                                                           ((n, s) for n, (s, _) in TEMPLATES.items())) +
                              "\n\nexamples:\n  dmz init chatbot\n  dmz init logic --dir fleet --name \"Chiller fleet\"")
    p.add_argument("template", nargs="?", choices=list(TEMPLATES), metavar="TEMPLATE",
                   help="one of: " + ", ".join(TEMPLATES))
    p.add_argument("--dir", default=".", help="where to write solution.yaml (default: here)")
    p.add_argument("--name", default=None, help="the project name used inside the file (default: the directory name)")
    p.add_argument("--force", action="store_true", help="overwrite an existing solution.yaml")
    p.add_argument("--list", action="store_true", help="list the templates and exit")
    p.set_defaults(func=init)


def init(args, ctx_factory) -> int:
    if args.list or not args.template:
        for name, (summary, _) in TEMPLATES.items():
            ui.say(f"  {ui.bold(name.ljust(9))} {summary}")
        if not args.template and not args.list:
            ui.hint("`dmz init <template>` writes one")
        return 0
    target_dir = Path(args.dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "solution.yaml"
    if target.exists() and not args.force:
        ui.error(f"{target} exists; pass --force to overwrite it")
        return 2
    project = args.name or target_dir.name.replace("-", " ").replace("_", " ").strip() or "my app"
    target.write_text(render(args.template, project=project))
    ui.say(ui.ok("wrote"), str(target), ui.dim(f"({args.template} template for {project!r})"))
    ui.say()
    ui.say(ui.kv([("next", "edit the tags and thresholds, then"),
                  ("", "dmz validate        # schema, references, guardrail preview"),
                  ("", "dmz plan            # the Change Set, read-only"),
                  ("", "dmz apply --approved-by <reviewer> --write-env app/.env"),
                  ("", "dmz verify          # the file's expectations, on the platform's evaluator")]))
    return 0
