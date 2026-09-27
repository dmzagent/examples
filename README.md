# examples

Runnable examples for the DMZAgent platform and its SDKs.

Six of them form a suite. Each is built the same way: a **governance file**
that declares what the platform should hold for the application, a **setup
script** that plans, applies and verifies it against your workspace, and an
**application** that runs against the platform. The governance file is code,
so what an application is allowed to do goes through the same review, history
and CI as the application itself. One command line, `dmz`, runs the loop:
setup, operations, and the platform's MCP server in front of any host.

## The suite

| | Example | What it shows | The model runs |
| --- | --- | --- | --- |
| 01 | [`01-hosted-chatbot`](01-hosted-chatbot) | The platform's own chat agent on a website. The widget, the breaker on its subject, holds that pause a conversation, and a live panel of the decisions behind each reply. | on the platform |
| 02 | [`02-platform-agent`](02-platform-agent) | The platform's agent runtime. An agent defined in code, trained and graded from scenarios, armed, dispatched and run. One step runs on your machine against data the platform never sees; every output passes a gate. | on the platform |
| 03 | [`03-ollama-chatbot`](03-ollama-chatbot) | Your own chatbot on an open-weights model through Ollama, with the SDK embedded: every turn recorded, the OWASP LLM Top 10 vocabulary, and a breaker check before each sensitive tool. | on your machine |
| 04 | [`04-review-desk`](04-review-desk) | Hold, review, release. The desk for the people behind governed agents: recoverable holds against hard stops, the review queue, manual overrides, ledger anchors, the posture ladder, remediation webhooks. | simulated agents |
| 05 | [`05-sensor-fleet`](05-sensor-fleet) | The deterministic logic engine, with no model at all. A versioned rulebook of predicates and half-life accumulators governs a fleet of chiller pumps; the controller checks each pump's breaker before its actuator runs. | nowhere |
| 06 | [`06-mcp-agent`](06-mcp-agent) | The platform's MCP server. An agent discovers the platform's tools over MCP, pre-flights every governed action and records it on the ledger, with a harness that does not take the model's word for it. The same server attached to Claude Code, Claude Desktop and Cursor. | on your machine |

The first three are the platform in its three shapes: hosted chatbot, hosted
agent, and a library inside an application you already have. 04 is what
happens *after* a policy fires, for the people who have to decide; 05 is the
same breaker and ledger driven by rules alone, for systems that act without
ever being asked a question; 06 is the platform as a tool provider for any
agent that speaks MCP.

[`microvm-containment-go`](microvm-containment-go) stands apart: a Go program
that boots one Firecracker microVM per task. It has its own README.

## How each example is laid out

```
NN-name/
  governance.py   the governance the application needs, as code
  setup.sh        plan → apply → verify against your workspace; writes app/.env
  app/            the application
  tests/          unit tests against fakes; no platform or key needed
  README.md       what it shows, how to run it, what to watch for
```

## The command line

[`cli/`](cli) holds `dmz`, the command line the examples are built around,
and `giaas`, the library the governance files are written for. Standard
library only; `pip install ./cli` puts `dmz` on PATH, and the setup scripts
run it from the clone without installing.

```
dmz auth set                        # paste a key once; saved for this machine
dmz doctor                          # key, endpoint, role, Canons, MCP server, Ollama
dmz init chatbot                    # a governance.py to start from
dmz plan && dmz apply               # reconcile the workspace with it
dmz check customer:alice            # may an agent act on this subject now?
dmz hold customer:alice --reason "chargeback dispute"
dmz reviews                         # what is waiting for a person
dmz mcp tools                       # what the platform's MCP server offers
dmz mcp config --client claude-code # attach it to Claude Code (or claude-desktop, cursor, ...)
```

The [CLI README](cli/README.md) is the reference for the commands, the
governance declarations, and the MCP bridge; each example's `governance.py`
is a worked one.

## Running an example

You need Python 3.11 or newer, a DMZAgent workspace, and a **tenant_admin**
API key for it from the console. Then:

```
export DMZAGENT_API_KEY=ck_...            # never commit this; setup writes app/.env (mode 0600)
export DMZAGENT_BASE_URL=https://...      # only if you are not on the default endpoint
cd 01-hosted-chatbot
./setup.sh                                # dmz plan, apply, verify; app/.env gets the ids and an app key
python3 app/serve.py                      # then open http://localhost:8000
```

`setup.sh` is safe to run again: an unchanged file plans as "unchanged", an
edited one as an update, and `dmz destroy` takes the example's resources
back out. `dmz doctor` says what is missing before you start.

Before you start:

- **One workspace per example.** Breaker policies apply to every subject in a
  workspace, and `verify` evaluates the workspace as it is, so two examples
  sharing one will see each other's rules in their results.
- **Canons are installed in the console.** An API key cannot install one.
  When a governance file requires a Canon the workspace lacks, `setup.sh`
  stops and says which, and where in the console to install it.
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

Every example and the toolkit have unit tests that run against in-process
fakes, so they need no platform, no key and no network beyond installing the
SDK:

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
- A `governance.py`, a `setup.sh` that runs `dmz`, and an `app/`, if it runs against the platform.
- A `README.md` that states what it shows, how to run it, and what to watch for.
- Tests against fakes, so CI can run them without a workspace.
- Pin the SDK explicitly, so the example keeps working when the SDK moves.
- No credentials in source. Read them from the environment; write only to a
  gitignored `.env`.
