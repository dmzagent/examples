# giaas — Governance Infrastructure as a Service

Declare the governance an application needs in one Python file. Plan it, apply
it, verify it, take it back out. The same file goes through your own code
review and CI, so the record of *who changed which policy, when, and who
approved it* is your Git history.

```
python -m giaas plan    governance.py   # read-only change set
python -m giaas apply   governance.py   # reconcile; write ids and the app key to .env
python -m giaas verify  governance.py   # requirements met? policies resolve as declared?
python -m giaas destroy governance.py   # remove what the file declares
```

Standard library only. Python 3.10 or newer. Nothing to install: put this
directory on `PYTHONPATH`, or `pip install -e giaas`.

## A governance file

```python
from giaas import Governance, presence, strength, settings

governance = Governance("support-bot", "Harbor Supply's support agent")

# Operator settings on the division. Typed keys are validated here.
governance.division_config(
    reasoning_mode="per_frame",                            # reason on every message
    enforcement_posture=settings.get("posture", "enforce"),  # observe | warn | enforce
)

# A Canon is a tag vocabulary plus starter policies. The workspace must have
# it installed for these tags to ever fire; API keys cannot install one, so
# this is a checked requirement with console instructions when it is missing.
governance.require_canon("cn_seed_openai_agent_safety",
                         why="prompt-injection, PII-leak and tool-misuse tags")

# Circuit-breaker rules: clauses over the subject's soul → breaker state.
governance.breaker_policy("Block on prompt injection",
    rules=[("rt_agent_prompt_injection_v1", ">=", 0.7)], action="block")

# Response policies from the closed catalog: enforce, coordinate, remediate.
governance.policy("Hold on scope creep",
    when=[strength("rt_agent_scope_creep_v1", ">=", 0.5)], lane="enforce", level="hold")
governance.policy("Open a review on scope creep",
    when=[presence("rt_agent_scope_creep_v1")], lane="coordinate", level="review")

# A least-privilege key for the application, minted once, written to .env.
governance.sdk_key("support-bot app", env_var="DMZAGENT_APP_KEY")

# Policy tests, run by the platform's own evaluator.
governance.expect("scope creep is held and reviewed",
    labels={"rt_agent_scope_creep_v1": 0.8}, enforce="hold", coordinate="review")
```

`settings` holds values passed as `--var KEY=VALUE`, so the same file can be
applied in `observe` posture to a staging workspace and `enforce` to production.

## What it manages

| Declaration | Platform resource | Identity |
|---|---|---|
| `division_config(...)` | the division's configuration blob (merged, never replaced wholesale) | the division |
| `require_canon(id)` | an installed Canon — verified; installed only where the deployment lets keys do it | canon id |
| `breaker_policy(name, ...)` | a circuit-breaker policy (`/v1/cb/policies`) | name |
| `policy(name, ...)` | a response policy in the unified engine (`/v1/policies`) | name |
| `chatbot(agent_name, ...)` | a hosted chat agent definition (`/v1/chatbot-definitions`) | agent name |
| `logic_rulebook(name, slug, rulebook)` | a private Logic Canon, republished as a new version when its rules change and installed into the workspace | slug |
| `sdk_key(label, env_var)` | an analyst-role application key (`/v1/agent-stream/api-keys`) | label, tracked in local state |

Identity is by name. A renamed declaration creates a new resource; `destroy`
with the old file removes the old one.

## Credentials and what each can do

The applier uses **one tenant_admin API key** (`DMZAGENT_API_KEY`), minted in
the console under Team & access, bound to the workspace it governs. It reads
the workspace and division from the key itself. The platform keeps some
lifecycle operations for humans signed into the console, and the toolkit says
so rather than working around it:

- installing Canons, creating workspaces and divisions, registering webhook
  subscriptions, and revoking keys are console operations;
- the key it mints for the application is written once to the env file
  (mode 0600) and never to the state file or the terminal.

The platform rate-limits per vendor (free tier: 10 requests a second). The
client paces itself and waits out a 429 instead of failing an apply halfway.

## Files it writes

- `.env` (or `--env-file`): `DMZAGENT_BASE_URL`, `DMZAGENT_WORKSPACE_ID`,
  `DMZAGENT_DIVISION_ID`, any `governance.env(...)` outputs, and minted keys.
- `.giaas/<name>.state.json`: the ids the platform assigned. No secrets. A
  `.gitignore` is written beside it.

## In CI

`plan --markdown` prints a change set for a pull request comment; `apply`
runs on merge. Because the applier is idempotent, re-running it is safe, and
because `verify` uses the platform's own policy evaluator, the tests in the
governance file are tests of what production will do.

```yaml
- run: PYTHONPATH=giaas python -m giaas plan governance.py --markdown | tee plan.md
  env: { DMZAGENT_API_KEY: ${{ secrets.DMZAGENT_STAGING_KEY }} }
```

## Tests

```
python -m unittest discover -s tests
```

The suite drives the engine against an in-memory fake of the operator
surface whose response shapes were checked against a live server.
