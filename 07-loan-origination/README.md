# 07 · A loan origination engine with people at every gate

Five agents work a loan file from application to a decision package:
intake, verification, conditions & remediation, compliance, and the
decision package. **None of them approves a loan.** This example declares
the governance they run under as one Solution Manifest:

- a deterministic **rulebook** over every event the loan system emits;
- **responses** that hold a file for a person, block a regulatory breach, or
  send a missing-document condition back to the loan system to be remediated;
- a **reasoning** workspace over what the agents themselves do;
- the **people**: underwriters and closers decide, and compliance audits
  holding no other role.

Remediation is origination's exception loop. A file that fails a check is
held, not failed. The conditions agent asks for what is missing and re-checks
when it arrives. Four failed re-checks inside 72 hours hand the condition to a
person.

```
solution.yaml      the manifest: the `lending-controls` rulebook (11 rules), two
                   workspaces, 17 policies and a breaker, roles, outputs,
                   12 expectations
tests/events.json  synthetic loan-file events and the tags each must produce,
                   including the near-misses that must produce none
```

`setup.sh` and `app/` (the five agents over synthetic files) come next. The
agent spine is the `uni/agent-sdk/examples/loan_intake.py` intake agent,
extended the way `02-platform-agent` runs one.

## The rules

| Rule | Fires when | The engine does |
|---|---|---|
| `dti-over-policy` / `ltv-over-policy` | DTI > 43%, LTV > 97% | **hold** for an underwriter, and **review** |
| `docs-incomplete` | after intake: < 2 pay stubs, no W-2, or no photo ID | **webhook** a condition to the loan system |
| `income-unverified` | stated vs verified income differ > 10% | **hold** |
| `condition-stalled` | a condition fails re-check; accumulates, 72 h half-life | at strength 4, **escalate** to a person |
| `trid-loan-estimate-late` | Loan Estimate > 3 business days after application (Reg Z 1026.19(e)) | **block** and **escalate** |
| `adverse-action-without-reasons` | a denial or counteroffer with no reasons (Reg B 1002.9) | **block** |
| `adverse-action-late` | adverse action notice > 30 days | **escalate** |
| `hmda-incomplete-at-decision` | a decision with HMDA data missing (Reg C) | **hold** |
| `approval-by-preparer` | the approver prepared the file | **block** |
| `approval-by-agent` | an agent tried to approve | **block** and **escalate** |

The DSL compares a field with a constant, not with another field. So the loan
system's adapter computes `approval.by_preparer` and `approval.by_agent` and
sends them on the event. Maker-checker on the business act lives in the
rulebook today, because the platform enforces it only on manifest apply.

## What `apply` builds today

The platform has provisioners for divisions, workspaces, corpora (through the
workspace), Logic Canons, breakers, policies and chatbots. Everything else in
this file is **declared and recorded**: `apply` anchors it on the ledger and
`plan` diffs it, but nothing provisions it yet. That covers valuation
sources, crosswalks, sources, connectors, role bindings, outputs and the
deployment target. Outputs have an emitter and a cadence, and no deliverer is
wired, so an emission is recorded as pending.

## What was checked

The manifest was run through the platform's own code, offline:

- `manifest.validate_manifest`;
- `logic_dsl.compile_rulebook`, with every event in `tests/events.json`
  evaluated through the compiled rulebook;
- `manifest_guardrails.check_guardrails` at its default floors;
- every expectation through `policy_engine._conditions_match`,
  `resolve_most_restrictive` and `cb.evaluate`, judged the way `dmz verify`
  judges the platform's answer.

All passed.

Each check was then broken on purpose, and a named failure caught every one of
the ten breaks:

- a threshold loosened;
- the stage gate dropped;
- a hold downgraded;
- the agent-approval block removed;
- the escalation threshold lowered;
- the auditor removed;
- the auditor made non-independent;
- a canon unpinned;
- a breaker pointed at an undeclared workspace;
- the incident playbook changed.

What only a live platform can confirm: that
`library/cn_seed_openai_agent_safety@latest` emits the `rt_agent_*` labels
these policies watch, and that the loan system's webhook answers.
