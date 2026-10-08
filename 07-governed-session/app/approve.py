"""The operator's side of a held call: see what waits, decide it, read the
record.

An approval is the call the agent was about to make, verbatim, and the
words of the rule that held it. Deciding it needs a person's name: the API
key identifies this integration, and an approval decided by the integration
that asked for it has recorded nobody.

    python3 app/approve.py                                # what is waiting
    python3 app/approve.py apr_7f3c approve --actor dana  # approve one
    python3 app/approve.py apr_7f3c decline --actor dana --reason "use the vendored copy"
    python3 app/approve.py --behaviors seat:billing-agent # the agent's conduct record
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from agent import AGENT, client, load_env
from dmzagent import ConflictError, DMZAgent

HERE = Path(__file__).resolve().parent


def pending(cx: DMZAgent, say=print) -> list:
    page = cx.list_approvals(status="pending")
    if not page.approvals:
        say("nothing is waiting")
    for a in page:
        tool = a.action.get("tool", "?")
        args = a.action.get("args") or {}
        shown = args.get("command") or args.get("path") or args
        say(f"{a.approval_id}  {a.subject_id}  {tool}: {shown}")
        say(f"    {a.reason or 'held'} · expires {a.expires_at} · on expiry: {a.on_expiry}")
    return list(page)


def decide(cx: DMZAgent, approval_id: str, decision: str, actor: str,
           reason: str | None, say=print) -> str:
    try:
        a = cx.decide_approval(approval_id, decision, actor_id=actor, reason=reason)
    except ConflictError as exc:
        # Someone decided first, or the window closed. Not a fault to retry.
        status = exc.body.get("status", "decided") if isinstance(exc.body, dict) else "decided"
        say(f"{approval_id} was already {status}")
        return status
    say(f"{a.approval_id} {a.status} by {a.decision.actor_id if a.decision else actor}")
    return a.status


def behaviors(cx: DMZAgent, subject: str, say=print) -> list:
    page = cx.list_behaviors(subject)
    for b in page:
        calls = f" on {', '.join(b.calls)}" if b.calls else ""
        say(f"{b.observed_at}  {b.polarity:8}  {b.tag}  strength {b.strength:.2f}"
            f"  ({b.source}){calls}")
    if not page.behaviors:
        say("no behaviors recorded yet")
    return list(page)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("approval_id", nargs="?")
    ap.add_argument("decision", nargs="?", choices=["approve", "decline"])
    ap.add_argument("--actor", default=getpass.getuser(),
                    help="who is deciding: your own id for this person (default: your login)")
    ap.add_argument("--reason")
    ap.add_argument("--behaviors", nargs="?", const=AGENT, metavar="SUBJECT",
                    help="print a subject's conduct record instead")
    args = ap.parse_args()

    env = load_env(HERE / ".env")
    key = env.get("DMZAGENT_APP_KEY")
    if not key:
        print("app/.env has no DMZAGENT_APP_KEY; run ./setup.sh first", file=sys.stderr)
        return 2
    cx = client(key, env)
    if args.behaviors:
        behaviors(cx, args.behaviors)
    elif args.approval_id and args.decision:
        decide(cx, args.approval_id, args.decision, args.actor, args.reason)
    elif args.approval_id:
        ap.error("say approve or decline")
    else:
        pending(cx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
