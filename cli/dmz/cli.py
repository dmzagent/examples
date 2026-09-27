"""The ``dmz`` command tree and its error handling."""
from __future__ import annotations

import argparse
import sys

from giaas.client import ConfigError, PlatformError
from giaas.engine import RequirementUnmet
from giaas.resources import DeclarationError

from . import __version__, ui
from .commands import auth, completion, doctor, gov, init_cmd, mcp_cmd, ops
from .config import resolve
from .mcp.client import McpError

EXIT_OK = 0          # done
EXIT_FAILED = 1      # ran, and the answer is "no": a failed verify, a breaker that does not allow
EXIT_USAGE = 2       # a configuration or declaration problem on this side
EXIT_PLATFORM = 3    # the platform refused a request
EXIT_UNREACHABLE = 4 # the platform could not be reached

EXAMPLES = """\
examples:
  dmz auth set                       save a key (pasted, never typed on the command line)
  dmz whoami                         who the key is: workspace, role, division
  dmz doctor                         is everything in place to run an example?
  dmz init chatbot                   write a governance.py to start from
  dmz plan && dmz apply              reconcile the workspace with governance.py
  dmz check customer:alice           may the agent act on this subject right now?
  dmz hold customer:alice --reason "chargeback dispute"
  dmz reviews                        what is waiting for a person
  dmz mcp tools                      what the platform's MCP server offers
  dmz mcp config --client claude-code   attach it to Claude Code

exit codes: 0 done · 1 the answer is no (verify failed, breaker not allowing)
            2 configuration or declaration problem · 3 the platform refused · 4 unreachable
"""


class Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str):
        super().__init__(prog, max_help_position=30, width=100)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dmz", formatter_class=Formatter, epilog=EXAMPLES,
        description="The DMZAgent command line: govern a workspace from a file, operate breakers "
                    "and reviews, and put the platform's MCP server in front of any MCP host.")
    parser.add_argument("--version", action="version", version=f"dmz {__version__}")
    parser.add_argument("--base-url", metavar="URL",
                        help="the platform endpoint (default: $DMZAGENT_BASE_URL, then https://api.dmzagent.com)")
    parser.add_argument("--env-file", metavar="FILE",
                        help="read the key and ids from this env file (default: $DMZAGENT_ENV_FILE, ./app/.env, ./.env)")
    parser.add_argument("--profile", metavar="NAME", help="the saved profile to use (default: $DMZ_PROFILE or 'default')")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                        help="colour output (default: auto; NO_COLOR is honoured)")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress lines on stderr")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for module in (auth, doctor, init_cmd, gov, ops, mcp_cmd, completion):
        module.register(sub, Formatter)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui.configure(args.color)
    quiet = args.quiet or bool(getattr(args, "json", False))
    log = (lambda msg: None) if quiet else ui.note

    def ctx_factory():
        return resolve(base_url=args.base_url, env_file=args.env_file, profile=args.profile, log=log)

    try:
        return int(args.func(args, ctx_factory) or 0)
    except ConfigError as exc:
        ui.error(str(exc))
        if "could not reach" in str(exc):
            ui.hint("check --base-url (or DMZAGENT_BASE_URL) and your network; `dmz doctor` runs the checks")
            return EXIT_UNREACHABLE
        if "API key" in str(exc) or "ck_" in str(exc):
            ui.hint("`dmz auth set` saves a key for this machine; `dmz whoami` shows which key is in use")
        return EXIT_USAGE
    except (DeclarationError, RequirementUnmet) as exc:
        ui.error(str(exc))
        return EXIT_USAGE
    except PlatformError as exc:
        explain(exc)
        return EXIT_PLATFORM
    except McpError as exc:
        ui.error(f"the MCP server answered with an error: {exc}")
        if exc.meaning:
            ui.hint(exc.meaning)
        return EXIT_PLATFORM
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        return EXIT_OK


def explain(exc: PlatformError) -> None:
    """A platform refusal, in words that say what to do."""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    where = f"{exc.method} {exc.path}"
    if exc.status == 401:
        ui.error(f"the key was rejected ({where})")
        ui.hint("the key may be revoked or belong to another deployment; `dmz whoami` shows which key is in use")
    elif exc.status == 403:
        if "dashboard" in detail.lower() or "cookie" in detail.lower() or "console" in detail.lower():
            ui.error(f"this operation is console-only: API keys cannot do it ({where})")
            ui.hint("open the console for this one; everything else in the workflow stays in the terminal")
        else:
            ui.error(f"the key's role cannot do this ({where}): {detail}")
            ui.hint("apply needs tenant_admin; overrides and reviews need analyst; reads need viewer")
    elif exc.status == 404:
        ui.error(f"not found ({where}): {detail}")
        ui.hint("check the id, and that --base-url points at the deployment the key belongs to")
    elif exc.status == 429:
        ui.error(f"the rate limit did not clear ({where})")
        ui.hint("the free tier allows a short burst of ten requests a second and 120 a minute per vendor")
    elif exc.status >= 500:
        ui.error(f"the platform had an internal error ({where}): {detail}")
        ui.hint("retry once; if it persists, report the error id above to support")
    else:
        ui.error(f"the platform refused a request ({where}): {detail}")
