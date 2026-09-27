# 04 · Hold, review, release: an operations desk for governed agents

Three of Harbor Supply's back-office agents act on the company's behalf: a
payments agent settles supplier invoices, a records agent exports customer
data, a support agent handles returns. Each of their sensitive actions is
guarded with the SDK. This example is the other side of that guard: the
desk where a person sees why an agent was paused, decides, and releases it,
with every transition anchored on the ledger.

```
solution.yaml     the Solution Manifest: what each kind of trouble does. A
                  hard stop for a leak, a recoverable hold plus a review for
                  tool misuse, a flag for scope creep, an escalation for
                  deception, a remediation directive to the company's own
                  endpoint; the posture ladder as a variable
setup.sh          validate → plan → apply → mint the desk's key → verify;
                  installs the SDK
app/desk.py       the desk: subjects and breakers, the review queue,
                  decisions with anchors, remediation directives received
app/simulate.py   the three agents' scripted traffic, through the SDK,
                  with guard(raise_on_open=True) around each sensitive action
```

## Run it

```bash
export DMZAGENT_API_KEY=ck_...           # a tenant_admin key for the workspace
export DMZ_APPROVED_BY=reviewer          # who approved the change (maker-checker); in CI, the merger
./setup.sh --var posture=observe         # start by watching; decisions are recorded, nothing stops
python3 app/desk.py                      # http://localhost:8002
python3 app/simulate.py --loop 30        # in another terminal
```

When the rate of holds on real traffic is understood, apply the same file
in enforce posture. Nothing else changes; the plan shows one line, and the
Stack gains a version whose applier, approver and ledger anchor `dmz stack`
lists:

```bash
./setup.sh --var posture=enforce
#  Plan: 0 to add, 1 to change, 0 to replace, 0 to remove, 9 unchanged
#    ~ division                 main  config
```

Through the GitOps workflow the same change is a pull request that edits
the default in the file: the Change Set is posted on the PR, and the merge
applies it with the author as maker and the merger as checker.

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

## What the manifest declares

| Section of `solution.yaml` | Effect |
|---|---|
| `divisions[main].config.enforcement_posture: ${posture:-enforce}` | `observe` → `warn` → `enforce`: the ladder for turning enforcement on without surprises |
| `corpora[desk-corpus].reasoning_canons` | the PII-leak, tool-misuse, scope-creep and deception vocabulary, installed by apply |
| `circuit_breaker_policies`: `block` on PII leak | a hard stop: what has left cannot be recalled |
| `policies`: `enforce/hold` + `coordinate/review` on tool misuse | the pause a person can end, and the queue item that lets them |
| `circuit_breaker_policies`: `review` on scope creep | half-open: flagged on every check, never stopped |
| `policies`: `coordinate/escalate` on deception | the people who own the agent are told |
| `policies`: `remediate/webhook` with `config.delivery` | the company's own playbook, driven by the platform; `--var remediation_url=...` points it at a real endpoint |
| `roles`: an auditor on the division | the segregation of duties the vendor guardrail requires |
| `expectations` | a leak blocks and remediates, tool misuse holds and asks, scope creep only flags, deception escalates |

One analyst key, minted by `setup.sh` with `dmz keys mint`, serves the desk
and the simulator: reads, review actions, overrides, events; no policy
edits.

## Honest limits

- Reviews and holds come from the platform's reasoning over the recorded
  traffic. On a deployment without inference nothing gets tagged; the desk's
  own hold and engage buttons still exercise every path, including the
  simulator's refusal.
- `resolve` records the decision and feeds the soul; it does not move the
  breaker. `release` does. The desk shows both on the same row on purpose.
- Webhook subscriptions (the platform-wide feed) are registered in the
  console; the declared per-policy delivery target is what the manifest
  manages.

## Tests

```
pip install -r requirements.txt
python3 -m unittest discover -s tests
```

The desk's reads and actions against a fake platform, the remediation hook,
and the simulator's guard: the action runs when closed and is refused, on the
record, when held.
