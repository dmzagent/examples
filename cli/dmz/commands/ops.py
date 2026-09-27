"""Operate breakers and reviews from the terminal: ``dmz check|hold|release|
engage|states|decisions|reviews|watch``."""
from __future__ import annotations

import os
import sys
import time
import urllib.parse

from .. import ui
from ..config import canonical_subject

REVIEW_ACTIONS = ("claim", "resolve", "release", "hold", "escalate")


def register(sub, formatter) -> None:
    c = sub.add_parser("check", help="may an agent act on this subject right now?", formatter_class=formatter,
                       epilog="examples:\n  dmz check customer:alice\n  dmz check subject:dv_1:chat-agent:emb_42 --json\n"
                              "\nexit 0 when the breaker allows, 1 when it does not.")
    c.add_argument("subject", help="a logical id (customer:alice) or a canonical subject id")
    c.add_argument("--json", action="store_true", help="the raw check result")
    c.set_defaults(func=check)

    for action, help_text in (("hold", "pause a subject's breaker (recoverable; a person releases it)"),
                              ("release", "release a held or open breaker back to closed"),
                              ("engage", "open a subject's breaker outright (a hard stop)")):
        p = sub.add_parser(action, help=help_text, formatter_class=formatter,
                           epilog=f"example:\n  dmz {action} customer:alice --reason \"chargeback dispute\"")
        p.add_argument("subject")
        p.add_argument("--reason", required=True, help="recorded on the ledger with the transition")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=override, action=action)

    s = sub.add_parser("states", help="breaker states in the workspace", formatter_class=formatter)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=states)

    d = sub.add_parser("decisions", help="breaker transitions, newest first", formatter_class=formatter,
                       epilog="examples:\n  dmz decisions --limit 20\n  dmz decisions --follow      # tail new transitions")
    d.add_argument("--limit", type=int, default=15)
    d.add_argument("--subject", help="only this subject (logical or canonical id)")
    d.add_argument("--follow", action="store_true", help="keep polling and print new transitions")
    d.add_argument("--interval", type=float, default=3.0, help="seconds between polls with --follow")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=decisions)

    r = sub.add_parser("reviews", help="what is waiting for a person, and act on it", formatter_class=formatter,
                       epilog="examples:\n  dmz reviews\n  dmz reviews --status all\n"
                              "  dmz reviews claim rv_123\n  dmz reviews resolve rv_123 --decision accepted\n"
                              "  dmz reviews release rv_123 --reason \"false positive\"\n"
                              "  dmz reviews escalate rv_123 --to-tier division")
    r.add_argument("review_action", nargs="?", choices=REVIEW_ACTIONS, metavar="ACTION",
                   help="claim | resolve | release | hold | escalate (omit to list)")
    r.add_argument("review_id", nargs="?", metavar="REVIEW_ID")
    r.add_argument("--status", default="open", help="open | claimed | resolved | dismissed | all (default: open)")
    r.add_argument("--decision", default=None, help="with resolve: accepted | dismissed | resolved (default: resolved)")
    r.add_argument("--reason", default=None, help="with release, hold, escalate")
    r.add_argument("--to-tier", default="division", help="with escalate (default: division)")
    r.add_argument("--unclaim", action="store_true", help="with claim: give the review back")
    r.add_argument("--limit", type=int, default=50)
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=reviews)

    w = sub.add_parser("watch", help="a live view of states, open reviews and recent transitions",
                       formatter_class=formatter)
    w.add_argument("--interval", type=float, default=3.0)
    w.add_argument("--once", action="store_true", help="print one snapshot and exit")
    w.set_defaults(func=watch)


# -- helpers -----------------------------------------------------------------

def _subject(ctx, text: str) -> str:
    if text.startswith("subject:"):
        return text
    if not ctx.division_id:
        ctx.whoami()
    return canonical_subject(ctx.division_id, text)


def _workspace(ctx) -> str:
    if not ctx.workspace_id:
        ctx.whoami()
    return ctx.workspace_id


def _when(ts: str | None) -> str:
    return (ts or "")[:19].replace("T", " ")


def _policy_name(p: dict) -> str:
    if p.get("manual"):
        return f"manual override by {p.get('by', '?')}"
    return p.get("name") or p.get("cb_policy_id") or p.get("reason") or "?"


def _by(decision: dict) -> str:
    fired = decision.get("fired_policies") or []
    for p in fired:
        if p.get("manual"):
            return f"{p.get('by', 'manual')}: {p.get('reason', '')}"
        if p.get("name") or p.get("cb_policy_id"):
            return p.get("name") or p.get("cb_policy_id")
    return decision.get("reason") or ""


# -- commands ----------------------------------------------------------------

def check(args, ctx_factory) -> int:
    ctx = ctx_factory()
    subject = _subject(ctx, args.subject)
    result = ctx.platform.post("/v1/cb/check", {"scope": "subject", "scope_ref": subject})
    if args.json:
        ui.print_json(result)
    else:
        allow = bool(result.get("allow"))
        ui.say(ui.kv([
            ("subject", subject),
            ("breaker", ui.breaker(result.get("state"), bool(result.get("warning")))),
            ("allow", ui.ok("yes") if allow else ui.fail("no")),
            ("held", "yes" if result.get("held") else "no"),
            ("reason", result.get("reason") or ""),
            ("policies", ", ".join(_policy_name(p) for p in result.get("fired_policies") or []) or ui.dim("none")),
            ("anchor", (result.get("anchor") or {}).get("ledger_event_id") if isinstance(result.get("anchor"), dict) else (result.get("anchor") or ui.dim("none"))),
        ]))
    return 0 if result.get("allow") else 1


def override(args, ctx_factory) -> int:
    ctx = ctx_factory()
    subject = _subject(ctx, args.subject)
    result = ctx.platform.post(f"/v1/cb/{args.action}", {
        "workspace_id": _workspace(ctx), "subject_id": subject, "reason": args.reason})
    if args.json:
        ui.print_json(result)
        return 0
    # The server answers with the breaker row under `state`.
    row = result.get("state") if isinstance(result.get("state"), dict) else result
    state = row.get("state") if isinstance(row, dict) else None
    verb = {"hold": "held", "release": "released", "engage": "engaged"}[args.action]
    ui.say(ui.ok(verb), subject, ui.dim("→"), ui.breaker(state))
    anchor = (row or {}).get("ledger_event_id") if isinstance(row, dict) else None
    if anchor:
        ui.say(ui.dim(f"  ledger {anchor}"))
    return 0


def states(args, ctx_factory) -> int:
    ctx = ctx_factory()
    data = ctx.platform.get("/v1/cb/states", workspace_id=_workspace(ctx), limit=args.limit)
    rows = data.get("states") or []
    if args.json:
        ui.print_json(rows)
        return 0
    ui.say(ui.table(["subject", "state", "since", "reason"],
                    [(r.get("scope_ref"), ui.breaker(r.get("state")), _when(r.get("transitioned_at") or r.get("updated_at")),
                      ui.short(r.get("reason") or "", 60)) for r in rows]))
    return 0


def decisions(args, ctx_factory) -> int:
    ctx = ctx_factory()
    subject = _subject(ctx, args.subject) if args.subject else None
    seen: set[str] = set()

    def fetch() -> list[dict]:
        data = ctx.platform.get("/v1/cb/decisions", workspace_id=_workspace(ctx), limit=args.limit)
        rows = data.get("decisions") or []
        if subject:
            rows = [r for r in rows if r.get("scope_ref") == subject]
        return rows

    rows = fetch()
    if args.json and not args.follow:
        ui.print_json(rows)
        return 0
    _print_decisions(rows)
    seen.update(r.get("decision_id", "") for r in rows)
    if not args.follow:
        return 0
    ui.note(f"following; new transitions every {args.interval:g}s (Ctrl-C stops)")
    while True:
        time.sleep(args.interval)
        fresh = [r for r in fetch() if r.get("decision_id") not in seen]
        if fresh:
            _print_decisions(list(reversed(fresh)), header=False)
            seen.update(r.get("decision_id", "") for r in fresh)


def _print_decisions(rows: list[dict], header: bool = True) -> None:
    table_rows = [(_when(r.get("created_at")), ui.short(r.get("scope_ref") or "", 44),
                   f"{ui.breaker(r.get('state_before'))} → {ui.breaker(r.get('state_after'))}",
                   ui.short(_by(r), 50), ui.short(r.get("ledger_event_id") or "", 8)) for r in rows]
    if header:
        ui.say(ui.table(["when", "subject", "transition", "by", "ledger"], table_rows))
    else:
        for row in table_rows:
            ui.say("  " + "  ".join(row))


def reviews(args, ctx_factory) -> int:
    ctx = ctx_factory()
    if args.review_action:
        if not args.review_id:
            ui.error(f"reviews {args.review_action} needs a REVIEW_ID")
            return 2
        return _review_action(args, ctx)
    query = {"workspace_id": _workspace(ctx), "limit": args.limit}
    if args.status and args.status != "all":
        query["status"] = args.status
    data = ctx.platform.get("/v1/reviews", **query)
    rows = data.get("reviews") or []
    if args.json:
        ui.print_json(rows)
        return 0
    ui.say(ui.table(["review", "subject", "tag", "level", "status", "claimed by", "opened"],
                    [(r.get("review_id"), ui.short(r.get("subject_id") or "", 40), r.get("tag_id") or "",
                      r.get("level") or "", r.get("status"), r.get("claimed_by") or ui.dim("—"),
                      _when(r.get("created_at"))) for r in rows]))
    if rows and not args.review_action:
        ui.hint("`dmz reviews claim <id>` takes one; `resolve`, `release`, `hold`, `escalate` decide it")
    return 0


def _review_action(args, ctx) -> int:
    rid = urllib.parse.quote(args.review_id, safe="")
    action = args.review_action
    if action == "claim":
        result = ctx.platform.post(f"/v1/reviews/{rid}/claim", {"release": bool(args.unclaim)})
    elif action == "resolve":
        status = args.decision or "resolved"
        if status not in ("resolved", "dismissed", "accepted"):
            ui.error("--decision must be accepted, dismissed or resolved")
            return 2
        result = ctx.platform.post(f"/v1/reviews/{rid}/resolve", {"status": status, "decision": status})
    else:
        payload = {"reason": args.reason or f"{action} from dmz"}
        if action == "escalate":
            payload["to_tier"] = args.to_tier
        result = ctx.platform.post(f"/v1/reviews/{rid}/{action}", payload)
    if args.json:
        ui.print_json(result)
        return 0
    review = result.get("review") or result
    ui.say(ui.ok(action), args.review_id, ui.dim("→"), review.get("status") or "done")
    return 0


def watch(args, ctx_factory) -> int:
    ctx = ctx_factory()
    ws = _workspace(ctx)
    tty = sys.stdout.isatty()
    while True:
        states_ = ctx.platform.get("/v1/cb/states", workspace_id=ws, limit=30).get("states") or []
        reviews_ = ctx.platform.get("/v1/reviews", workspace_id=ws, status="open", limit=20).get("reviews") or []
        decisions_ = ctx.platform.get("/v1/cb/decisions", workspace_id=ws, limit=8).get("decisions") or []
        if tty and not args.once:
            sys.stdout.write("\033[2J\033[H")
        ui.say(ui.bold(f"workspace {ws}"), ui.dim(time.strftime("%H:%M:%S")))
        ui.say()
        ui.say(ui.bold("breakers"))
        ui.say(ui.table(["subject", "state", "since", "reason"],
                        [(ui.short(r.get("scope_ref") or "", 48), ui.breaker(r.get("state")),
                          _when(r.get("transitioned_at") or r.get("updated_at")), ui.short(r.get("reason") or "", 48))
                         for r in states_ if r.get("state") != "closed"] or []))
        ui.say()
        ui.say(ui.bold("open reviews"))
        ui.say(ui.table(["review", "subject", "tag", "level", "claimed by"],
                        [(r.get("review_id"), ui.short(r.get("subject_id") or "", 40), r.get("tag_id") or "",
                          r.get("level") or "", r.get("claimed_by") or ui.dim("—")) for r in reviews_]))
        ui.say()
        ui.say(ui.bold("recent transitions"))
        _print_decisions(decisions_)
        if args.once:
            return 0
        time.sleep(args.interval)
