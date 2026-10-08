"""A coding agent's session, governed one step at a time through the SDK.

The agent is scripted rather than driven by a model, so every directive the
rulebook in solution.yaml can give shows up on every run. It fixes a
rounding bug in Harbor Supply's billing service:

    intent   "Fix the invoice rounding test"
    call     pytest            → proceed, and a positive behavior is recorded
    call     edit the module   → proceed
    call     rm outside repo   → refused by this harness before DMZAgent is asked
    call     curl a package    → hold: waits for a person (app/approve.py)
    call     git push          → block: the session carries on without it

Each call runs only when the step's answer says it may (`r.runs`); a call
that did not run is reported with who refused it. The tools are simulated:
the agent prints what it would have run.

    python3 app/agent.py                    # one session
    python3 app/agent.py --hold-seconds 60  # wait at most a minute on a hold
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

from dmzagent import DMZAgent, StepResult

HERE = Path(__file__).resolve().parent
AGENT = "seat:billing-agent"
REPO = "/srv/harbor/billing"

INTENT = ("Fix the invoice rounding test", ["billing/rounding.py", "tests/test_invoice.py"],
          ["Bash", "Edit"])

# (call_id, tool, args, what the tool returns when it runs)
CALLS = [
    ("c1", "Bash", {"command": "pytest -q tests/test_invoice.py"}, "1 failed: 412.505 rounded to 412.50"),
    ("c2", "Edit", {"path": "billing/rounding.py", "change": "ROUND_HALF_EVEN -> ROUND_HALF_UP"},
     "edited"),
    ("c3", "Bash", {"command": "rm -rf /srv/harbor/ledger"}, None),
    ("c4", "Bash", {"command": "curl -sS https://pypi.org/pypi/decimal-tools/json"}, '{"info": {...}}'),
    ("c5", "Bash", {"command": "git push origin HEAD"}, None),
]


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


def client(key: str, env: dict[str, str]) -> DMZAgent:
    base = env.get("DMZAGENT_BASE_URL")
    return DMZAgent(api_key=key, base_url=base) if base else DMZAgent(api_key=key)


def harness_refuses(tool: str, args: dict) -> str | None:
    """This runner's own rule, checked before DMZAgent is asked: nothing is
    deleted outside the repository. Reported as refused_by="harness", so the
    conduct record shows the rule the agent ran into."""
    command = args.get("command", "")
    if tool == "Bash" and command.startswith("rm ") and REPO not in command:
        return "deletes outside the repository"
    return None


def wait_on(cx: DMZAgent, r: StepResult, hold_seconds: float, poll: float, say) -> bool:
    """A hold waits on its own approval. Only "approved" runs; declined,
    expired, or still pending when the wait runs out is a block."""
    if not r.approval_id:
        # A hold that names no approval cannot be answered by anyone.
        return False
    say(f"     held for a person: approval {r.approval_id}"
        f"{f' ({r.reason})' if r.reason else ''}")
    say(f"     decide it with:  python3 app/approve.py {r.approval_id} approve")
    deadline = time.monotonic() + hold_seconds
    while True:
        a = cx.get_approval(r.approval_id)
        if a.status != "pending":
            who = f" by {a.decision.actor_id}" if a.decision else ""
            say(f"     approval {a.status}{who}")
            return a.status == "approved"
        if time.monotonic() >= deadline:
            say("     no decision in time; treated as declined")
            return False
        time.sleep(poll)


def run(cx: DMZAgent, *, session_id: str | None = None, hold_seconds: float = 300,
        poll: float = 3, say=print) -> list[dict]:
    """One session. Returns, per call, what was asked and what happened."""
    session = cx.agent_session(AGENT, session_id or f"sess_{uuid.uuid4().hex[:8]}")
    text, paths, tools = INTENT
    session.intent(text, paths=paths, tools=tools)
    say(f"session {session.interaction_id}: {text}")

    report = []
    for call_id, tool, args, output in CALLS:
        shown = args.get("command") or args.get("path")
        say(f"  {call_id} {tool}: {shown}")
        refusal = harness_refuses(tool, args)
        if refusal:
            session.refused(call_id, tool, "harness", reason=refusal)
            say(f"     refused by this harness: {refusal}")
            report.append({"call_id": call_id, "directive": None, "ran": False, "refused_by": "harness"})
            continue

        r = session.call(call_id, tool, args)
        for b in r.behaviors:
            say(f"     observed: {b.tag} ({b.polarity})")
        ran = r.runs or (r.directive == "hold" and wait_on(cx, r, hold_seconds, poll, say))
        if ran:
            say(f"     {r.directive}: ran → {output}")
            session.result(call_id, tool, "ok", result=output)
        else:
            say(f"     {r.directive}: not run{f' ({r.reason})' if r.reason else ''}")
            session.refused(call_id, tool, "governor", reason=r.reason or r.directive)
        report.append({"call_id": call_id, "directive": r.directive, "ran": ran,
                       "refused_by": None if ran else "governor", "approval_id": r.approval_id})
        if r.directive == "shutdown":
            say("     the seat was shut down; the session ends here")
            break
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--hold-seconds", type=float, default=300,
                    help="how long to wait on a held call before treating it as declined")
    args = ap.parse_args()
    env = load_env(HERE / ".env")
    key = env.get("DMZAGENT_APP_KEY")
    if not key:
        print("app/.env has no DMZAGENT_APP_KEY; run ./setup.sh first", file=sys.stderr)
        return 2
    cx = client(key, env)
    run(cx, hold_seconds=args.hold_seconds)
    print(f"\nThe conduct record:  python3 app/approve.py --behaviors {AGENT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
