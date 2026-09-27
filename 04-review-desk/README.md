# 04 · Hold, review, release: an operations desk for governed agents

Three of Harbor Supply's back-office agents act on the company's behalf: a
payments agent settles supplier invoices, a records agent exports customer
data, a support agent handles returns. Each of their sensitive actions is
guarded with the SDK. This example is the other side of that guard: the
desk where a person sees why an agent was paused, decides, and releases it,
with every transition anchored on the ledger.

```
governance.py     what each kind of trouble does: a hard stop for a leak, a
                  recoverable hold plus a review for tool misuse, a flag for
                  scope creep, an escalation for deception, a remediation
                  directive to the company's own endpoint; the posture ladder
setup.sh          plan → apply → verify; installs the SDK
app/desk.py       the desk: subjects and breakers, the review queue,
                  decisions with anchors, remediation directives received
app/simulate.py   the three agents' scripted traffic, through the SDK,
                  with guard(raise_on_open=True) around each sensitive action
```

## Run it

```bash
export DMZAGENT_API_KEY=ck_...           # a tenant_admin key for the workspace
./setup.sh --var posture=observe         # start by watching; decisions are recorded, nothing stops
python3 app/desk.py                      # http://localhost:8002
python3 app/simulate.py --loop 30        # in another terminal
```

When the rate of holds on real traffic is understood, apply the same file
in enforce posture. Nothing else changes; the plan shows one line:

```bash
./setup.sh --var posture=enforce
#  ~ division_config  division   enforcement_posture: observe → enforce
```

## What the desk shows

- **Subjects and their breakers** — `closed`, `half_open` (flagged), `hold`
  (paused, releasable) or `open` (stopped). Hold or engage a subject
  yourself; release it when you have looked.
- **Review queue** — what the `coordinate` lane asked a person to look at.
  Claim it so two operators do not work the same item; confirm or dismiss it
  (a dismissal weakens the tag on the subject's soul, so a false positive
  teaches the system); release the subject from the same row; escalate to
  the division's admins.
- **Breaker decisions** — every transition, the policy or person behind it,
  and its ledger anchor. The full hash-chained ledger and its verification
  are in the console.
- **Remediation directives received** — when the platform's outbound
  delivery is armed by its operator, the "Remediate a PII leak" policy
  delivers its directive to `POST /hooks/remediate` on this desk. Until it
  is armed the platform records each attempt as `not_armed`, which is the
  honest state.

## What the simulator does

Each script is a short exchange and one sensitive action:

| Agent | Behaviour in the script | Action guarded |
|---|---|---|
| support-agent | ordinary replacement | `shipment.create_replacement` |
| payments-agent | moves money it was never asked to and self-approves | `payment.settle` |
| records-agent | exports names, addresses, phone numbers and card digits, then repeats one | `records.export` |

`guard(raise_on_open=True)` runs the action when the breaker is closed or
half-open and raises when it is held or open. A refusal is recorded as the
tool's result, so the ledger shows the action that did not happen and why.
With reasoning running on the platform, the last two scripts are the ones
that get tagged and held; with the desk you release them.

## What the governance declares

| Declaration | Effect |
|---|---|
| `enforcement_posture` from `--var posture` | `observe` → `warn` → `enforce`: the ladder for turning enforcement on without surprises |
| breaker `block` on PII leak | a hard stop: what has left cannot be recalled |
| `enforce/hold` + `coordinate/review` on tool misuse | the pause a person can end, and the queue item that lets them |
| breaker `review` on scope creep | half-open: flagged on every check, never stopped |
| `coordinate/escalate` on deception | the people who own the agent are told |
| `remediate/webhook` with a declared delivery target | the company's own playbook, driven by the platform |
| `sdk_key(...)` | one analyst key for the desk and the simulator: reads, review actions, overrides, events; no policy edits |

## Honest limits

- Reviews and holds come from the platform's reasoning over the recorded
  traffic. On a deployment without inference nothing gets tagged; the desk's
  own hold and engage buttons still exercise every path, including the
  simulator's refusal.
- `resolve` records the decision and feeds the soul; it does not move the
  breaker. `release` does. The desk shows both on the same row on purpose.
- Webhook subscriptions (the platform-wide feed) are registered in the
  console; the declared per-policy delivery target is what this file can
  manage with a key.

## Tests

```
pip install -r requirements.txt
python3 -m unittest discover -s tests
```

The desk's reads and actions against a fake platform, the remediation hook,
and the simulator's guard: the action runs when closed and is refused, on the
record, when held.
