"""The invoice-triage agent, defined in code.

Read this file top to bottom and you have read the agent. A step marked
`where="client"` is code: its body runs on this machine, against files the
platform never sees, and only what it returns crosses. A cloud step is
intent: its docstring is the instruction the platform's harness gives the
model; its body never leaves this machine and never runs (write `...`).
"""
from __future__ import annotations

import re
from pathlib import Path

from dmz_agent import CLIENT, Agent

INVOICES = Path(__file__).resolve().parent / "invoices"

agent = Agent(
    "Invoice triage",
    charter=(
        "You triage supplier invoices for Harbor Supply's accounts-payable team. "
        "Sort each invoice into auto-approve, needs-approval or hold-for-review, "
        "explain each decision in one sentence, and never invent an invoice you "
        "were not shown. You cannot pay anything: a person does that."
    ),
    privilege="read_only",
    # An output schema binds what the agent may hand back: a triage report.
    output_schemas=[{"name": "triage_report", "modality": "file",
                     "required": ["decisions", "summary"]}],
)


@agent.step(where=CLIENT, client_timeout_seconds=120)
def scan_inbox(inputs: dict) -> dict:
    """List the invoices waiting in the accounts-payable inbox."""
    # Runs HERE. The platform receives the fields below and nothing else:
    # not the files, not the folder path, not this code.
    folder = Path(inputs.get("path") or INVOICES)
    found = []
    for file in sorted(folder.glob("*.txt")):
        text = file.read_text()
        amount = re.search(r"^Total:\s*\$?([\d,]+\.?\d*)", text, re.M)
        supplier = re.search(r"^Supplier:\s*(.+)$", text, re.M)
        po = re.search(r"^PO:\s*(\S+)", text, re.M)
        found.append({
            "invoice": file.stem,
            "supplier": supplier.group(1).strip() if supplier else "unknown",
            "amount": float(amount.group(1).replace(",", "")) if amount else None,
            "has_po": bool(po and po.group(1).upper() != "NONE"),
        })
    return {"invoices": found, "count": len(found)}


@agent.step(needs=[scan_inbox], max_turns=4)
def triage():
    """For every invoice in the list you were given, decide: auto-approve when
    the amount is under $500 and it carries a purchase order; needs-approval
    when it is $500 or more and carries a purchase order; hold-for-review when
    it has no purchase order at all. Return, in `result`, a `decisions` list of
    {invoice, decision, reason} and a one-paragraph `summary`."""
    ...


@agent.step(needs=[triage], max_turns=2)
def draft_note():
    """Write a short note to the accounts-payable team: how many invoices were
    auto-approved, how many await approval, and which ones are held and why.
    Plain words, no more than six sentences. Return it as `result.note`."""
    ...


# The decisions the agent must make deterministically. These are its training
# scenarios: each is a fact about a shaped input and the output it must give.
# The platform compiles them into a decision table, replays them to grade the
# agent's consistency, and refuses to arm an agent that grades below B.
SCENARIOS = [
    {"name": "small invoice with a PO auto-approves",
     "match": [{"field": "amount", "op": "<", "value": 500}, {"field": "has_po", "op": "==", "value": True}],
     "output": {"decision": "auto-approve"}},
    {"name": "large invoice with a PO needs approval",
     "match": [{"field": "amount", "op": ">=", "value": 500}, {"field": "has_po", "op": "==", "value": True}],
     "output": {"decision": "needs-approval"}},
    {"name": "no purchase order is held for review",
     "match": [{"field": "has_po", "op": "==", "value": False}],
     "output": {"decision": "hold-for-review"}},
]
