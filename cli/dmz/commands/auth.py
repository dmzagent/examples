"""``dmz auth set|status|clear`` and ``dmz whoami``."""
from __future__ import annotations

import getpass
import os
import sys

from ..client import ConfigError, Platform, fingerprint

from .. import ui
from ..config import DEFAULT_BASE_URL, credentials_path, delete_profile, load_credentials, save_profile


def register(sub, formatter) -> None:
    p = sub.add_parser("auth", help="save, show or forget an API key", formatter_class=formatter,
                       description="Keys are saved per profile in a file only you can read "
                                   "(~/.config/dmz/credentials.json, mode 0600). They are pasted, "
                                   "never passed on the command line, so they stay out of shell history.")
    s = p.add_subparsers(dest="auth_command", metavar="<action>", required=True)
    a = s.add_parser("set", help="save a key read from stdin", formatter_class=formatter,
                     epilog="examples:\n  dmz auth set                      # prompts, input hidden\n"
                            "  dmz auth set < key.txt            # from a file\n"
                            "  dmz --profile staging auth set --base-url https://staging.example")
    a.add_argument("--base-url", dest="auth_base_url", metavar="URL",
                   help=f"the endpoint this key belongs to (default: {DEFAULT_BASE_URL})")
    a.add_argument("--no-verify", action="store_true", help="save without checking the key against the platform")
    a.set_defaults(func=auth_set)
    s.add_parser("status", help="which profiles are saved (fingerprints only)", formatter_class=formatter
                 ).set_defaults(func=auth_status)
    c = s.add_parser("clear", help="forget the profile's key", formatter_class=formatter)
    c.set_defaults(func=auth_clear)

    w = sub.add_parser("whoami", help="the key in use: principal, workspace, role, division",
                       formatter_class=formatter)
    w.add_argument("--json", action="store_true", help="machine-readable output")
    w.set_defaults(func=whoami)


def auth_set(args, ctx_factory) -> int:
    if sys.stdin.isatty():
        key = getpass.getpass("Paste the API key (input hidden): ")
    else:
        key = sys.stdin.readline()
    key = key.strip()
    if not key.startswith("ck_"):
        raise ConfigError("that is not a DMZAgent API key (they start with ck_); nothing saved")
    base_url = (args.auth_base_url or args.base_url or os.environ.get("DMZAGENT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    profile = args.profile or "default"
    who = None
    if not args.no_verify:
        who = Platform(key, base_url).whoami()
    path = save_profile(profile, key, base_url)
    ui.say(ui.ok("saved"), f"key {fingerprint(key)} as profile {profile!r} in {path}")
    if who:
        ui.say(ui.kv([("workspace", who["workspace_id"]), ("role", who["role"]), ("endpoint", base_url)]))
    return 0


def auth_status(args, ctx_factory) -> int:
    profiles = load_credentials()["profiles"]
    if not profiles:
        ui.say(ui.dim(f"no saved profiles ({credentials_path()})"))
        ui.hint("`dmz auth set` saves one; DMZAGENT_API_KEY in the environment works without one")
        return 0
    rows = [(name, fingerprint(p.get("api_key", "")), p.get("base_url", ""), p.get("saved_at", ""))
            for name, p in sorted(profiles.items())]
    ui.say(ui.table(["profile", "key", "endpoint", "saved"], rows))
    return 0


def auth_clear(args, ctx_factory) -> int:
    profile = args.profile or "default"
    if delete_profile(profile):
        ui.say(ui.ok("forgot"), f"profile {profile!r}")
        ui.hint("the key itself is still valid; revoke it in the console if it should not be")
    else:
        ui.say(ui.dim(f"no profile {profile!r} to forget"))
    return 0


def whoami(args, ctx_factory) -> int:
    ctx = ctx_factory()
    who = ctx.whoami()
    if args.json:
        ui.print_json({**who, "key": ctx.fingerprint(), "key_source": ctx.key_source, "base_url": ctx.base_url})
        return 0
    ui.say(ui.kv([
        ("key", f"{ctx.fingerprint()}  {ui.dim('from ' + ctx.key_source)}"),
        ("principal", who.get("user_id") or "?"),
        ("vendor", who.get("vendor_id") or "?"),
        ("workspace", who["workspace_id"]),
        ("role", role_line(who["role"])),
        ("division", who.get("division_id") or ui.dim("unknown (set DMZAGENT_DIVISION_ID)")),
        ("endpoint", ctx.base_url),
    ]))
    return 0


def role_line(role: str) -> str:
    can = {
        "tenant_admin": "apply governance, operate, run applications",
        "analyst": "operate breakers and reviews, run applications; apply needs tenant_admin",
        "auditor": "read; overrides and applications need analyst",
        "viewer": "read; overrides and applications need analyst",
    }.get(role, "")
    return f"{role}  {ui.dim(can)}" if can else role
