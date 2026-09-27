"""Walk the invoice-triage agent through the platform's agent runtime.

    python3 app/run.py            # everything below, in order
    python3 app/run.py dogma      # just print the compiled manifest

Steps, each printed as it happens:
  1. deploy    create the agent and push its dogma (the compiled manifest)
  2. train     add the scenarios; grade determinism; show the arm gate
  3. dispatch  deterministic auto-mode: a trained input, no model call
  4. run       arm the agent and run a job with the client step served here
  5. outputs   the output gate: a file flows, an unbound write is blocked
  6. record    the agent's actions go on the governance record, then check()
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agent import INVOICES, SCENARIOS, agent  # noqa: E402
from dmz_agent import AgentApiError, AgentClient, Runner, arm_blocker  # noqa: E402


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


def say(step: str, text: str) -> None:
    print(f"\n[{step}] {text}")


def main(argv: list[str]) -> int:
    env = load_env(HERE / ".env")
    if argv[:1] == ["dogma"]:
        print(json.dumps(agent.compile(), indent=2))
        return 0
    missing = [k for k in ("DMZAGENT_APP_KEY", "DMZAGENT_WORKSPACE_ID", "DMZAGENT_DIVISION_ID") if not env.get(k)]
    if missing:
        print(f"app/.env is missing {', '.join(missing)} — run ./setup.sh first", file=sys.stderr)
        return 2
    client = AgentClient(env["DMZAGENT_APP_KEY"], env.get("DMZAGENT_BASE_URL", "https://api.dmzagent.com"))
    workspace_id, division_id = env["DMZAGENT_WORKSPACE_ID"], env["DMZAGENT_DIVISION_ID"]
    state_file = HERE / ".agent.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}

    # 1. deploy ---------------------------------------------------------------
    dogma = agent.compile()
    agent_id = state.get("agent_id")
    if agent_id:
        try:
            client.get_agent(agent_id)
        except AgentApiError:
            agent_id = None
    if not agent_id:
        created = client.create_agent(agent.name, division_id=division_id, workspace_id=workspace_id)
        agent_id = created["agent_id"]
        state["agent_id"] = agent_id
        state_file.write_text(json.dumps(state, indent=2))
    before = client.get_agent(agent_id)
    if before.get("dogma_hash") and json.dumps(before.get("dogma"), sort_keys=True) == json.dumps(dogma, sort_keys=True):
        say("deploy", f"{agent_id}: dogma v{before['dogma_version']} already matches this file")
    else:
        after = client.set_dogma(agent_id, dogma)
        say("deploy", f"{agent_id}: dogma v{after['dogma_version']} pushed ({after.get('dogma_hash', '')[:19]}…)")
        if before.get("dogma_hash"):
            print("         the change voided the previous grade: the agent is disarmed until re-graded")
    nodes = dogma["spine"]["nodes"]
    print("         spine: " + " → ".join(f"{n['id']}({'client' if n.get('where') == 'client' else 'cloud'})" for n in nodes))
    print("         only instructions crossed; the bodies of scan_inbox and the cloud steps did not")

    # 2. train ----------------------------------------------------------------
    current = client.get_agent(agent_id)
    say("train", f"arm gate before training: {arm_blocker(current) or 'armed'}")
    have = {s["name"] for s in client.list_scenarios(agent_id)}
    for scenario in SCENARIOS:
        if scenario["name"] not in have:
            client.add_scenario(agent_id, **scenario)
    grade = client.grade_from_training(agent_id)
    print(f"         {grade['scenario_count']} scenarios replayed through the compiled table: "
          f"grade {grade['grade']}, {grade['variance_pct']}% variance, {grade['conflict_count']} conflicts")

    # 3. dispatch -------------------------------------------------------------
    for inputs in ({"amount": 412.5, "has_po": True}, {"amount": 1880.0, "has_po": True},
                   {"amount": 965.0, "has_po": False}, {"amount": 12.0, "currency": "EUR"}):
        d = client.dispatch(agent_id, inputs)
        verdict = d.get("output", {}).get("decision") if d.get("mode") == "deterministic" else "escalate to a person"
        say("dispatch", f"{json.dumps(inputs)} → {d.get('mode')}: {verdict}"
            + (f"  (scenario: {d['matched_scenario']})" if d.get("matched_scenario") else ""))
    print("         a trained input answers from the decision table with no model call; off-path input escalates")

    # 4. run ------------------------------------------------------------------
    armed = client.arm(agent_id)
    say("run", f"armed: status={armed['status']} grade={armed['grade']} pinned to dogma v{armed['graded_version']}")
    with Runner(agent, client) as runner:
        print(f"         runner session {runner.session_id} open; serving {list(agent.client_steps())} from this machine")
        started = time.monotonic()
        try:
            job = client.run(agent_id, trigger={"path": str(INVOICES), "requested_by": "run.py"})
        except AgentApiError as exc:
            job = {"error": str(exc)}
        elapsed = time.monotonic() - started
        time.sleep(1.5)  # let the runner's last poll settle
    for served in runner.served:
        if served.get("status") == "done":
            print(f"         client step {served['node']} ran here and returned "
                  f"{served['output'].get('count')} invoices: "
                  + ", ".join(i['invoice'] for i in served['output'].get('invoices', [])))
        elif served.get("status"):
            print(f"         client step {served.get('node')}: {served['status']}")
    job_id = job.get("job_id")
    if job_id:
        record = client.job(job_id)
        print(f"         job {job_id}: {record.get('status')} in {elapsed:.1f}s")
        for step in record.get("steps") or []:
            out = step.get("output") or {}
            line = f"           {step.get('node_id'):<14} {step.get('status'):<9}"
            if step.get("status") == "error":
                line += f" {str(step.get('error') or step.get('detail'))[:110]}"
            elif isinstance(out, dict) and out:
                line += " " + json.dumps(out)[:110]
            print(line)
        if record.get("status") == "error":
            print("         a step failed and the spine halted there — the job record above is the replayable evidence.")
            print("         (cloud steps need the platform's inference; a workspace without a model key stops here)")
    else:
        print(f"         run refused: {job.get('error')}")

    # 5. outputs --------------------------------------------------------------
    allowed = client.gate_output(agent_id, modality="file", name="triage_report",
                                 payload={"decisions": [], "summary": "example report"})
    say("outputs", f"file output → {allowed['status']} on the {allowed['lane']} lane: {allowed.get('detail')}")
    invalid = client.gate_output(agent_id, modality="file", name="triage_report", payload={"summary": "no decisions"})
    print(f"         schema-invalid file output → {invalid['status']}: {invalid.get('detail')}")
    blocked = client.gate_output(agent_id, modality="remediation", action_ref="erp.pay_invoice",
                                 payload={"invoice": "INV-2041", "amount": 412.5})
    print(f"         write to an unbound target → {blocked['status']}: {blocked.get('detail')}")
    print(f"         pending human confirmations: {len(client.pending_outputs(agent_id))} "
          "(a write through a bound connector would wait here for a person)")

    # 6. record ---------------------------------------------------------------
    try:
        from dmzagent import DMZAgent
    except ImportError:
        say("record", "the dmzagent SDK is not installed; skipping the governance record (pip install -r requirements.txt)")
        return 0
    cx = DMZAgent(api_key=env["DMZAGENT_APP_KEY"], base_url=client.base_url)
    subject = f"subject:{division_id}:journey:invoice-triage"
    ack = cx.observation(agent_subject_id=subject, subject_type="journey",
                         subjects=[{"subject_id": subject, "role": "agent", "kind": "agent"}],
                         payload={"kind": "agent_job", "agent_id": agent_id, "job_id": job_id,
                                  "status": (job.get("status") or job.get("error", "")[:80]),
                                  "invoices": [i["invoice"] for s in runner.served for i in (s.get("output") or {}).get("invoices", [])]})
    say("record", f"job recorded on the agent stream as frame {ack.frame_id} (accepted={ack.accepted})")
    check = cx.check(subject_id=subject)
    print(f"         breaker for {subject}: {check.state} (allow={check.allow}) — {check.reason}")
    print("         the policies in solution.yaml decide this from what reasoning tags on that stream")
    cx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
