# 01 · The platform's own chatbot, governed from a file

Harbor Supply puts the platform's hosted chat agent on its support page. The
platform serves the widget and answers each turn, and before every reply it
runs the visitor's message through its reasoning pipeline and checks the
agent's own circuit breaker. This example declares what that breaker and the
lanes around it do — in `governance.py` — and shows the decisions next to the
widget as they happen.

```
governance.py   what the platform should hold: reasoning mode, posture, the
                agent-safety Canon, breaker rules, response lanes, the chat
                agent itself, and a least-privilege key for the site
setup.sh        plan → apply → verify, writing app/.env
app/serve.py    the customer's site: the widget plus a governance panel
```

## Run it

You need a workspace and a **tenant_admin API key** for it (console → Team &
access → API keys). Then:

```bash
export DMZAGENT_API_KEY=ck_...
./setup.sh                       # plan, apply, verify → app/.env
python3 app/serve.py             # http://localhost:8000
```

`setup.sh` prints the change set before applying it. On a second run every
line reads `=` (unchanged): the file is the source of truth and applying it
again changes nothing.

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
- **Policies in force**: the rules `governance.py` declared, read back from
  the platform.

Two buttons drive a **drill**: hold the agent, watch the widget answer with a
pause, release it. Both are operator overrides recorded on the ledger with
the reason you type.

## What the governance declares, and why

| Declaration | Effect |
|---|---|
| `division_config(reasoning_mode="per_frame")` | reason on every message, so the panel moves while you type (metered; `per_trace` batches per session) |
| `enforcement_posture` from `--var posture` | `observe` records what *would* happen; `enforce` lets it bite |
| `require_canon("cn_seed_openai_agent_safety")` | the vocabulary the rules below use; verified, with console instructions if missing |
| breaker `block` on deception / PII leak | the agent's own replies open its breaker |
| breaker `review` on scope creep | half-open: allowed, flagged, visible |
| `enforce/hold` + `coordinate/review` on scope creep | a recoverable pause and the queue item that lets a person release it |
| `coordinate/review` on escalation | a core-vocabulary tag, so this works before any Canon is installed |
| `chatbot(...)` | the hosted agent: name, site origin lock, system prompt, the protected action it must not perform itself |
| `sdk_key(...)` | an analyst-role key for `serve.py`; it reads state and can hold/release, never edit policy |
| `expect(...)` | policy tests run by the platform's evaluator in `giaas verify` |

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
- The Canon install and the origin lock live in the console and the
  definition respectively; the site server holds no policy authority.

## Tests

```
python3 -m unittest discover -s tests
```

Runs the site server against a fake platform: the page embeds the right
agent and never exposes the key; the panel reads breaker state, reviews,
decisions and policies; the drill posts the operator overrides the platform
expects.
