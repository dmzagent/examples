# dmz — the DMZAgent command line

One command for the whole loop: declare governance in a file and reconcile a
workspace with it; check, hold, release and review from the terminal; put
the platform's MCP server in front of Claude Code, Claude Desktop, Cursor or
your own agent. Standard library only, Python 3.10 or newer.

```
pip install ./cli          # from this repository; `dmz` and `giaas` land on PATH
dmz auth set               # paste a key; saved for this machine, never on the command line
dmz doctor                 # is everything in place?
```

Without installing: `PYTHONPATH=cli python3 -m dmz ...`. The examples'
`setup.sh` scripts do that, so they run from a fresh clone.

## Commands

| Command | What it does |
| --- | --- |
| `dmz auth set` / `status` / `clear` | save a key to a profile (pasted, input hidden; `~/.config/dmz/credentials.json`, mode 0600), list profiles by fingerprint, forget one |
| `dmz whoami` | the key in use, where it came from, its principal, workspace, role and division |
| `dmz doctor` | Python, key, endpoint, role, workspace, division, installed Canons, the MCP server, Ollama, a governance file; each with what to do about it |
| `dmz init <template>` | write a `governance.py` to start from: `chatbot`, `agent`, `sdk-app`, `desk`, `logic`, `mcp` |
| `dmz plan` / `apply` / `verify` / `destroy` / `outputs` | governance as code, below |
| `dmz check <subject>` | may an agent act on this subject now? exit 0 when the breaker allows, 1 when not |
| `dmz hold` / `release` / `engage <subject> --reason` | a recoverable pause, its release, a hard stop; every transition lands on the ledger |
| `dmz states` / `decisions [--follow]` | breaker states; transitions newest first, or tailed live |
| `dmz reviews [claim\|resolve\|release\|hold\|escalate <id>]` | what waits for a person, and the decision |
| `dmz watch` | a live view: non-closed breakers, open reviews, recent transitions |
| `dmz mcp tools` / `resources` / `read` / `call` | the platform's MCP server from the terminal |
| `dmz mcp enforce <subject> <action>` / `record` | the two calls every governed agent makes; `enforce` exits 0 allow, 1 review, 3 block |
| `dmz mcp config --client ...` | the configuration that attaches the server to an MCP host |
| `dmz mcp bridge` | what a host launches: standard MCP on stdio, forwarded to the platform |
| `dmz completion bash\|zsh\|fish` | tab completion |

Every read command takes `--json`. Exit codes are the same everywhere: 0
done, 1 the answer is no, 2 a configuration or declaration problem on this
side, 3 the platform refused, 4 the platform could not be reached. Errors
say what to do next, not just what happened:

```
$ dmz apply
dmz: this operation is console-only: API keys cannot do it (POST /v1/corpus/install)
hint: open the console for this one; everything else in the workflow stays in the terminal
```

### Where the key comes from

In order: `DMZAGENT_API_KEY` (or `DMZAGENT_APP_KEY`) in the environment; an
env file, either `--env-file`, `$DMZAGENT_ENV_FILE`, `./app/.env` or `./.env`
(the file `dmz apply` writes for an application); a profile saved with `dmz
auth set` (`--profile`, `$DMZ_PROFILE`, else `default`). `dmz whoami` says
which one is in use. Subjects can be typed as the platform's MCP server
canonicalizes them: `customer:alice` becomes
`subject:<division>:customer:alice`.

## Governance as code

Declare the governance an application needs in one Python file. Plan it,
apply it, verify it, take it back out. The same file goes through code
review and CI, so the record of *who changed which policy, when, and who
approved it* is your Git history.

```
dmz plan                        # read-only change set
dmz apply --write-env app/.env  # reconcile; ids and the app key land in the env file
dmz verify                      # requirements met? policies resolve as declared?
dmz destroy                     # remove what the file declares
dmz plan --markdown             # a change set for a pull request comment
```

A governance file:

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

# A least-privilege key for the application, minted once, written to the env file.
governance.sdk_key("support-bot app", env_var="DMZAGENT_APP_KEY")

# Policy tests, run by the platform's own evaluator.
governance.expect("scope creep is held and reviewed",
    labels={"rt_agent_scope_creep_v1": 0.8}, enforce="hold", coordinate="review")
```

`settings` holds values passed as `--var KEY=VALUE`, so the same file can be
applied in `observe` posture to a staging workspace and `enforce` to
production. `dmz init` writes a file like this for each shape of
application.

### What it manages

| Declaration | Platform resource | Identity |
| --- | --- | --- |
| `division_config(...)` | the division's settings (reasoning mode, posture) | the division |
| `require_canon(id)` | a Canon installed in the workspace | canon id; console-only to install |
| `breaker_policy(name, rules, action)` | a circuit-breaker policy | name |
| `policy(name, when, lane, level)` | a response policy (enforce, coordinate, remediate) | name |
| `chatbot(agent_name, site_domain, ...)` | a hosted chat agent and its embed | agent name in the division |
| `logic_rulebook(name, slug, rulebook)` | a private Logic Canon, versioned and installed | slug |
| `sdk_key(label, env_var)` | an application key, minted once | label |
| `expect(name, labels, ...)` | a policy test run by `dmz verify` | name |

Identity is by name, so an edited rule plans as an update and a renamed one
as a delete plus a create. State is a small JSON file next to the
governance file (`.giaas/<name>.state.json`, gitignored) holding ids and
key *prefixes*; the minted secret goes only to the env file, mode 0600.

### Credentials and limits

`apply` and `destroy` need a **tenant_admin** key; operating (`check`,
`hold`, reviews, `mcp enforce`) needs analyst; reads need viewer. Canon
installs, workspace creation and key revocation are console operations for
API keys; the toolkit reports them as unmet requirements with instructions,
never fakes them. The free tier meters requests per vendor (a short burst of
ten a second, 120 a minute); every call here is paced and a 429 is waited
out, so a plan of forty resources takes a few seconds rather than failing.

## The MCP server

The platform's MCP server (`POST /mcp/v1`, bearer key) offers ten tools and
three resources: pre-flight a proposed action (`enforce_covenant`: a verdict
of allow, review or block plus a ledger anchor), record a decision on the
hash-chained ledger, search the installed Canons, read a subject's soul,
and read the workspace's policies, Canons and recent ledger.

```
dmz mcp tools
dmz mcp enforce customer:alice issue_refund --payload '{"amount": 50}' && ./refund.sh
dmz mcp record customer:alice refund_issued --actor human
dmz mcp read concordia:/workspace/recent-ledger --limit 20
```

### In front of Claude Code, Claude Desktop and Cursor

The server speaks its own protocol version ("1.0") and does not take the
`initialized` notification, so hosts cannot connect to it directly today.
`dmz mcp bridge` is a standard MCP server on stdio that forwards to it:
the dated protocol handshake, tool descriptors with read-only annotations,
structured results, application errors as tool results the model can read.
`dmz mcp config` writes the host configuration:

```
dmz --env-file app/.env mcp config --client claude-code
  → claude mcp add dmzagent -s user -- dmz mcp bridge --env-file /path/to/app/.env
dmz mcp config --client claude-desktop | cursor | windsurf | vscode | generic
  → JSON for the host's config file, and where that file is
```

The key never appears in the configuration: the bridge resolves it the
same way every other command does when the host starts it. The bridge is
tested with the official MCP Python SDK client and with a scripted host.

## Tests

```
PYTHONPATH=cli python3 -m unittest discover -s cli/tests
```

The toolkit against an in-memory platform; the command line over HTTP
against the same fake; the bridge in process, as a subprocess speaking
standard MCP, and through the official SDK when it is installed.
