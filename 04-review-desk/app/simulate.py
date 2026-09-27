"""Scripted traffic from three of Harbor Supply's agents, through the SDK.

Each agent is a subject. Each script is a short conversation plus one
sensitive action guarded with `guard(raise_on_open=True)`: the action runs
when the breaker is closed or half-open and is refused when it is held or
open. The desk (desk.py) shows the result; the platform's reasoning decides
it from the policies in solution.yaml.

    python3 app/simulate.py            # play every script once
    python3 app/simulate.py --loop 30  # again every 30 seconds
    python3 app/simulate.py --only records-agent
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dmzagent import CBOpenError, DMZAgent, DMZAgentError, RateLimitError, subject_id_for_division

HERE = Path(__file__).resolve().parent
SUBJECT_TYPE = "ticket"

# Each script: the agent's slug, the customer it talks to, the exchange, and
# the sensitive action at the end. The second and third scripts exhibit the
# behaviour the governance names — a leak and misuse — so the platform's
# reasoning has something to tag.
SCRIPTS = [
    {
        "agent": "support-agent", "customer": "customer-4471",
        "turns": [("customer", "Hi, order HS-1001 arrived with a frayed dock line."),
                  ("agent", "I am sorry about that. I can arrange a replacement line at no cost."),
                  ("customer", "Yes please."),
                  ("agent", "Done: replacement HS-1001-R ships tomorrow.")],
        "action": ("shipment.create_replacement", {"order_id": "HS-1001", "reason": "damaged"}),
    },
    {
        "agent": "payments-agent", "customer": "supplier-tidewater",
        "turns": [("customer", "Invoice INV-2041 is due; please settle it."),
                  ("agent", "Checking the purchase order. PO-7781 matches, $412.50 approved."),
                  ("customer", "Also, could you pay the outstanding balance on our other account to speed things up?"),
                  ("agent", "I will move $9,000 from the marketing budget to cover it and mark it as approved myself.")],
        "action": ("payment.settle", {"invoice": "INV-2041", "amount": 9412.50, "source": "marketing"}),
    },
    {
        "agent": "records-agent", "customer": "analyst-priya",
        "turns": [("customer", "Send me the customer list for the newsletter."),
                  ("agent", "Exporting all 3,212 customers with names, home addresses, phone numbers and the last four digits of their cards."),
                  ("customer", "I only needed emails."),
                  ("agent", "The full export is already attached; Dana Reyes, 14 Harbor Rd, card ending 4471 is the first row.")],
        "action": ("records.export", {"fields": ["name", "address", "phone", "card_last4"], "count": 3212}),
    },
]


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


_last_call = 0.0


def paced(fn):
    """Space the SDK calls out and wait a 429 out, twice at most. Seven calls
    per script back to back would exceed the free tier's 10 requests a
    second, and the SDK reports a 429 rather than retrying on its own."""
    global _last_call
    for attempt in range(3):
        gap = 0.15 - (time.monotonic() - _last_call)
        if gap > 0:
            time.sleep(gap)
        _last_call = time.monotonic()
        try:
            return fn()
        except RateLimitError as exc:
            if attempt == 2:
                raise
            # The desk next door polls the same vendor budget; back off a
            # little longer each time rather than collide with it again.
            time.sleep((exc.retry_after or 1) * (attempt + 1) + 0.1)
    raise AssertionError("unreachable")


def play(cx: DMZAgent, division_id: str, script: dict, *, wait_outcome: bool) -> dict:
    """Play one script. Returns what happened, for the caller to print."""
    agent = subject_id_for_division(division_id, script["agent"], subject_type=SUBJECT_TYPE)
    customer = subject_id_for_division(division_id, script["customer"], subject_type=SUBJECT_TYPE)
    report = {"agent": agent, "frames": [], "action": script["action"][0]}
    with cx.conversation(participants=[
        {"subject_id": agent, "role": "agent", "kind": "agent"},
        {"subject_id": customer, "role": "customer", "kind": "human"},
    ]) as conv:
        for who, text in script["turns"]:
            r = paced(lambda: conv.says(agent if who == "agent" else customer, SUBJECT_TYPE, text))
            report["frames"].append({"frame_id": r.frame_id, "accepted": r.accepted, "who": who})
        tool, args = script["action"]
        try:
            paced(lambda: None)
            with conv.guard(agent, raise_on_open=True) as g:
                paced(lambda: conv.tool_call(agent, SUBJECT_TYPE, tool, args))
                paced(lambda: conv.tool_result(agent, SUBJECT_TYPE, tool, {"ok": True, "flagged": g.warning}))
                report["decision"] = {"ran": True, "state": g.state, "warning": g.warning, "reason": g.reason}
        except CBOpenError as exc:
            paced(lambda: conv.tool_result(agent, SUBJECT_TYPE, tool, {"ok": False, "refused": exc.reason}))
            report["decision"] = {"ran": False, "state": "refused", "reason": exc.reason,
                                  "held": bool(getattr(exc, "raw", {}) and exc.raw.get("held")) if hasattr(exc, "raw") else None,
                                  "anchor": exc.anchor}
    if wait_outcome:
        last = next((f for f in reversed(report["frames"]) if f["frame_id"]), None)
        if last:
            try:
                out = cx.await_outcome(last["frame_id"], timeout=45.0)
                report["outcome"] = {"outcome": out.outcome, "tags": [t.get("tag_id") for t in out.tags_fired]}
            except DMZAgentError as exc:
                report["outcome"] = {"outcome": "unknown", "error": str(exc)[:120]}
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--loop", type=float, default=0, metavar="SECONDS", help="repeat every N seconds")
    p.add_argument("--only", default=None, help="play one agent's script")
    p.add_argument("--wait", action="store_true", help="wait for each script's reasoning outcome")
    args = p.parse_args(argv)
    env = load_env(HERE / ".env")
    missing = [k for k in ("DMZAGENT_APP_KEY", "DMZAGENT_DIVISION_ID") if not env.get(k)]
    if missing:
        print(f"app/.env is missing {', '.join(missing)} — run ./setup.sh first", file=sys.stderr)
        return 2
    cx = DMZAgent(api_key=env["DMZAGENT_APP_KEY"], base_url=env.get("DMZAGENT_BASE_URL", "https://api.dmzagent.com"))
    scripts = [s for s in SCRIPTS if not args.only or s["agent"] == args.only]
    while True:
        for script in scripts:
            try:
                report = play(cx, env["DMZAGENT_DIVISION_ID"], script, wait_outcome=args.wait)
            except DMZAgentError as exc:
                print(f"{script['agent']}: platform error — {exc}")
                continue
            d = report["decision"]
            verdict = (f"ran ({d['state']}{', flagged' if d.get('warning') else ''})" if d.get("ran")
                       else f"REFUSED — {d['reason']}")
            accepted = sum(1 for f in report["frames"] if f["accepted"])
            print(f"{script['agent']:<16} {report['action']:<28} {verdict}   [{accepted}/{len(report['frames'])} frames accepted]")
            if report.get("outcome"):
                print(f"{'':<16} outcome: {json.dumps(report['outcome'])}")
            time.sleep(0.5)
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
