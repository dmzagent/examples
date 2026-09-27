# 01 · The platform's own chatbot, governed from a file

Harbor Supply puts the platform's hosted chat agent on its support page. The
platform serves the widget and answers each turn, and before every reply it
runs the visitor's message through its reasoning pipeline and checks the
agent's own circuit breaker. This example declares what that breaker and the
lanes around it do — in `solution.yaml`, a Solution Manifest the platform
itself validates, plans and applies — and shows the decisions next to the
widget as they happen.

```
solution.yaml   the Solution Manifest: reasoning mode and posture on the
                division, the agent-safety Canon in the workspace's corpus,
                breaker rules, response lanes, the chat agent itself, an
                auditor role, and the policy tests the platform must pass
setup.sh        validate → plan → apply → mint the site's key → verify,
                writing app/.env
app/serve.py    the customer's site: the widget plus a governance panel
```

## Run it

You need a workspace, a **tenant_admin API key** for it (console → Team &
access → API keys), and the name of the person approving the change: the
platform applies a manifest only with an approver distinct from the
applier (maker-checker). Then:

```bash
export DMZAGENT_API_KEY=ck_...
export DMZ_APPROVED_BY=reviewer  # who approved this change; in CI it is the PR's merger
./setup.sh                       # validate, plan, apply, mint the site's key, verify → app/.env
python3 app/serve.py             # http://localhost:8000
```

`setup.sh` prints the Change Set before applying it:

```
Plan: 11 to add, 0 to change, 0 to replace, 0 to remove, 0 unchanged
stack (unprovisioned) (new) · version 0 → 1
  + division                 main
  + corpus                   support-corpus
  + workspace                support
  + circuit_breaker_policy   block-deception
  ...
applied  version 1  stack stack_411c7a70271c
  ledger 1ba2e199-fb4f-4c5f-b832-413a054ff80c
```

The platform owns the Stack: its versions, who applied and who approved
each one, and the ledger anchor. On a second run the plan reads `11
unchanged` and apply answers `no change`; the file is the source of truth.
`dmz stack` shows the resources and their physical ids, `dmz drift` reports
anything changed behind the file's back, `dmz destroy` takes the example's
rules back out.

For a real site, apply with the domain the platform should answer from and
start in observe posture, where decisions are recorded but nothing is stopped:

```bash
./setup.sh --var site_domain=support.example.com --var posture=observe
```

## What you see

The page is two columns. Left, the platform's widget. Right, a panel the site
server fills from the platform with the key `setup.sh` minted:

- **The agent's circuit breaker**: `closed`, `half_open` (allowed, flagged),
  `hold` (paused, releasable) or `open` (blocked). The widget's reply path
  checks this before generating an answer; a held or open breaker turns every
  turn into a pause message.
- **Open reviews**: the human queue the `coordinate` lane fills. Confirming
  or dismissing one feeds the subject's soul back (a dismissal weakens the tag).
- **Recent breaker decisions**: each transition with the policy that caused
  it and its ledger anchor.
- **Policies in force**: the rules `solution.yaml` declared, read back from
  the platform.

Two buttons drive a **drill**: hold the agent, watch the widget answer with a
pause, release it. Both are operator overrides recorded on the ledger with
the reason you type.

## What the manifest declares, and why

| Section of `solution.yaml` | Effect |
|---|---|
| `divisions[main].config.reasoning_mode: per_frame` | reason on every message, so the panel moves while you type (metered; `per_trace` batches per session) |
| `enforcement_posture: ${posture:-enforce}` | `observe` records what *would* happen; `enforce` lets it bite; `--var posture=observe` picks |
| `corpora[support-corpus].reasoning_canons` | the agent-safety Canon, installed into the workspace by apply at its latest version |
| `workspaces[support]` adopting `${DMZAGENT_WORKSPACE_ID}` | the workspace the key is bound to; nothing is created, the Stack manages what is already yours |
| `circuit_breaker_policies`: `block` on deception / PII leak | the agent's own replies open its breaker |
| `circuit_breaker_policies`: `review` on scope creep | half-open: allowed, flagged, visible |
| `policies`: `enforce/hold` + `coordinate/review` on scope creep | a recoverable pause and the queue item that lets a person release it |
| `policies`: `coordinate/review` on escalation | a core-vocabulary tag, so this works before any Canon is installed |
| `chatbots[support-bot]` | the hosted agent: name, site origin lock, system prompt, the protected action it must not perform itself |
| `roles`: an auditor on the division | the vendor guardrail requires an independent auditor before an apply is accepted |
| `expectations` | policy tests run by the platform's evaluator in `dmz verify` |

The site's key is not in the file: `setup.sh` mints it with `dmz keys mint`
(an analyst key; it reads state and can hold and release, never edit policy)
and writes it to `app/.env`, where the site server reads it.

The breaker policies evaluate the **agent's** soul: the platform ingests the
bot's replies as the agent's own utterances, so a reply that leaks a card
number is a fact about the agent. The visitor gets a soul too; a policy on
visitor behaviour would express itself as reviews, not as a stop, because the
widget checks the agent, not the visitor.

## Honest limits

- `check()` before a reply reads the breaker's **cached state from earlier
  reasoning**; it never inspects the message being sent right now. A pause
  means the agent already tripped a policy, not that this message was judged.
- Reasoning runs on the platform's inference; in test or self-hosted setups
  without an inference key the widget cannot answer and the platform returns
  a gateway error, while the governance reads still work.
- The site server holds no policy authority. The Canon install, the origin
  lock and every rule come from the manifest, applied with a tenant_admin
  key under maker-checker; the site's own key can only read and override.

## Tests

```
python3 -m unittest discover -s tests
```

Runs the site server against a fake platform: the page embeds the right
agent and never exposes the key; the panel reads breaker state, reviews,
decisions and policies; the drill posts the operator overrides the platform
expects.
