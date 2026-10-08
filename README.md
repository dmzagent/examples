# examples

Runnable examples for the DMZAgent platform and its SDKs.

Seven of them form a suite. Each is built the same way: a **Solution
Manifest** (`solution.yaml`) that declares what the platform should hold for
the application, a **setup script** that validates, plans, applies and
verifies it through the platform's own GitOps wheel, and an **application**
that runs against the platform. The manifest is the platform's declarative
surface: the platform validates it, computes the Change Set against the
deployed Stack, refuses an apply without a second approver or an auditor,
provisions the resources, anchors every version on the ledger, and reports
drift. What an application is allowed to do therefore goes through the same
review, history and CI as the application itself. One command line, `dmz`,
runs the loop: the manifest wheel, operations, and the platform's MCP server
in front of any host.

## The suite

| | Example | What it shows | The model runs |
| --- | --- | --- | --- |
| 01 | [`01-hosted-chatbot`](01-hosted-chatbot) | The platform's own chat agent on a website. The widget, the breaker on its subject, holds that pause a conversation, and a live panel of the decisions behind each reply. | on the platform |
| 02 | [`02-platform-agent`](02-platform-agent) | The platform's agent runtime. An agent defined in code, trained and graded from scenarios, armed, dispatched and run. One step runs on your machine against data the platform never sees; every output passes a gate. | on the platform |
| 03 | [`03-ollama-chatbot`](03-ollama-chatbot) | Your own chatbot on an open-weights model through Ollama, with the SDK embedded: every turn recorded, the OWASP LLM Top 10 vocabulary, and a breaker check before each sensitive tool. | on your machine |
| 04 | [`04-review-desk`](04-review-desk) | Hold, review, release. The desk for the people behind governed agents: recoverable holds against hard stops, the review queue, manual overrides, ledger anchors, the posture ladder, remediation webhooks. | simulated agents |
| 05 | [`05-sensor-fleet`](05-sensor-fleet) | The deterministic logic engine, with no model at all. A versioned rulebook of predicates and half-life accumulators governs a fleet of chiller pumps; the controller checks each pump's breaker before its actuator runs. | nowhere |
| 06 | [`06-mcp-agent`](06-mcp-agent) | The platform's MCP server. An agent discovers the platform's tools over MCP, pre-flights every governed action and records it on the ledger, with a harness that does not take the model's word for it. The same server attached to Claude Code, Claude Desktop and Cursor. | on your machine |
| 07 | [`07-governed-session`](07-governed-session) | Agent mode. A coding agent asks before every tool call and runs it only on the answer: a push blocked, a network call held until a person approves it, a test run recorded as a positive behavior, and every refusal reported with who refused. | simulated agent |

The first three are the platform in its three shapes: hosted chatbot, hosted
agent, and a library inside an application you already have. 04 is what
happens *after* a policy fires, for the people who have to decide; 05 is the
same breaker and ledger driven by rules alone, for systems that act without
ever being asked a question; 06 is the platform as a tool provider for any
agent that speaks MCP; 07 is agent mode, where an agent asks before
every call and the governor's answer is the next step.

[`microvm-containment-go`](microvm-containment-go) stands apart: a Go program
that boots one Firecracker microVM per task. It has its own README.

## How each example is laid out

```
NN-name/
  solution.yaml   the Solution Manifest: the governance the application needs
  setup.sh        validate → plan → apply → mint the app's key → verify; writes app/.env
  app/            the application
  tests/          unit tests against fakes; no platform or key needed
  README.md       what it shows, how to run it, what to watch for
```

## The command line

[`cli/`](cli) holds `dmz`, the command line the examples are built around.
Standard library only; `pip install ./cli` puts `dmz` on PATH, and the setup
scripts run it from the clone without installing.

```
dmz auth set                        # paste a key once; saved for this machine
dmz doctor                          # key, endpoint, role, the manifest surface, MCP server, Ollama
dmz init chatbot                    # a solution.yaml to start from
dmz validate && dmz plan            # check the manifest; see the Change Set against the Stack
dmz apply --approved-by ravi        # reconcile (maker-checker: the approver is not the applier)
dmz keys mint --workspace support --label site --write-env app/.env
dmz verify                          # the file's expectations, on the platform's evaluator
dmz stack                           # the Stack: resources, physical ids, versions, approvers
dmz drift                           # has anything managed changed behind the file's back?
dmz check customer:alice            # may an agent act on this subject now?
dmz hold customer:alice --reason "chargeback dispute"
dmz reviews                         # what is waiting for a person
dmz mcp tools                       # what the platform's MCP server offers
dmz mcp config --client claude-code # attach it to Claude Code (or claude-desktop, cursor, ...)
```

The [CLI README](cli/README.md) is the reference for the commands, the
manifest, and the MCP bridge; each example's `solution.yaml` is a worked
manifest.

## The GitOps wheel

[`.github/workflows/governance.yml`](.github/workflows/governance.yml) is
the wheel the manifests are meant to turn in. A pull request that edits a
`solution.yaml` gets its Change Set posted as a comment (`dmz plan
--markdown`); merging it applies the manifest with the PR's author as maker
and the merger as checker. The platform refuses when they are the same
principal, so branch protection is what makes the four-eyes control sound.
The workflow needs a `DMZAGENT_API_KEY` repository secret (tenant_admin for
the workspace the manifests adopt) and skips, rather than fails, without it.

## Running an example

You need Python 3.11 or newer, a DMZAgent workspace, and a **tenant_admin**
API key for it from the console. Then:

```
export DMZAGENT_API_KEY=ck_...            # never commit this; setup writes app/.env (mode 0600)
export DMZ_APPROVED_BY=reviewer           # who approved the change; the platform refuses the applier's own name
export DMZAGENT_BASE_URL=https://...      # only if you are not on the default endpoint
cd 01-hosted-chatbot
./setup.sh                                # dmz validate, plan, apply, keys mint, verify; app/.env gets the ids and an app key
python3 app/serve.py                      # then open http://localhost:8000
```

`setup.sh` is safe to run again: an unchanged file plans as "unchanged" and
applies as "no change", an edited one as an update with a new Stack version,
and `dmz destroy` takes the example's rules back out (the workspace and its
data are retained). `dmz doctor` says what is missing before you start.

Before you start:

- **The platform must carry the manifest surface.** `dmz doctor` checks for
  it (`GET /v1/stacks`); a deployment without it answers 404 and the setup
  scripts cannot run there.
- **Every apply names two people.** `--approved-by` (or `DMZ_APPROVED_BY`)
  is the checker; the applier is the key's principal unless `--applied-by`
  says otherwise. The vendor guardrails also require an auditor role on the
  division, which every manifest here declares.
- **One workspace per example.** The manifests adopt the workspace the key
  is bound to (`${DMZAGENT_WORKSPACE_ID}`). Breaker policies apply to every
  subject in a workspace, and `verify` evaluates the workspace as it is, so
  two examples sharing one will see each other's rules in their results.
- **Canons are installed by apply.** Each manifest's corpus pins the Canons
  the rules need (`library/cn_...@latest`); the platform installs them into
  the workspace when the manifest is applied. Nothing is done in the console.
- **05 needs a logic workspace**; the others need a reasoning workspace.
- **03 and 06 need Ollama** running locally with a tool-capable model pulled.
- **MCP hosts need the bridge.** The platform's MCP server speaks its own
  protocol version, so Claude Code, Claude Desktop and Cursor connect through
  `dmz mcp bridge`; `dmz mcp config` writes their configuration.
- **The SDK is pinned to a commit.** The Python SDK is not on PyPI yet, so the
  `requirements.txt` in 02 to 05 installs it from its public repository at a
  fixed commit. Bump the pin deliberately.
- **Rate limits are real.** The free tier meters requests per vendor (a short
  burst of ten a second, 120 a minute). The SDK reports a 429 and does not
  retry, so every application here spaces its calls and waits a 429 out; the
  patterns are small and worth copying.

## Tests

Every example and the command line have unit tests that run against
in-process fakes (the CLI's fake platform carries the manifest engine, so the
whole validate → plan → apply → verify → drift → destroy wheel is exercised),
so they need no platform, no key and no network beyond installing the SDK:

```
PYTHONPATH=cli python3 -m unittest discover -s cli/tests -v
for d in 0*-*/; do (cd "$d" && python3 -m unittest discover -s tests -v); done
```

The same commands run in CI on every push and pull request
([`.github/workflows/tests.yml`](.github/workflows/tests.yml)).

## The SDKs

| SDK | Repository |
| --- | --- |
| Python | [`dmzagent/dmzagent-sdk-python`](https://github.com/dmzagent/dmzagent-sdk-python) |
| TypeScript | [`dmzagent/dmzagent-sdk-typescript`](https://github.com/dmzagent/dmzagent-sdk-typescript) |
| Java | [`dmzagent/dmzagent-sdk-java`](https://github.com/dmzagent/dmzagent-sdk-java) |
| C# | [`dmzagent/dmzagent-sdk-csharp`](https://github.com/dmzagent/dmzagent-sdk-csharp) |

The wire contract every SDK implements lives in
[`dmzagent/dmzagent-sdk-spec`](https://github.com/dmzagent/dmzagent-sdk-spec).
Conformance is proved there, by the shared vectors each SDK runs in its own
`spec-conformance` workflow; nothing in this repository duplicates that.

## Conventions for a new example

- One directory per example, named for what it demonstrates.
- A `solution.yaml`, a `setup.sh` that runs `dmz`, and an `app/`, if it runs against the platform.
- A `README.md` that states what it shows, how to run it, and what to watch for.
- Tests against fakes, so CI can run them without a workspace.
- Pin the SDK explicitly, so the example keeps working when the SDK moves.
- No credentials in source. Read them from the environment; write only to a
  gitignored `.env`.
