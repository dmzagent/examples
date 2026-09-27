# dmz — the DMZAgent command line

One command for the whole loop: declare governance in a Solution Manifest
and turn it through the platform's GitOps wheel (validate, plan, apply,
verify, drift); check, hold, release and review from the terminal; put the
platform's MCP server in front of Claude Code, Claude Desktop, Cursor or your
own agent. Standard library only, Python 3.10 or newer.

```
pip install ./cli          # from this repository; `dmz` lands on PATH
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
| `dmz doctor` | Python, key, endpoint, role, workspace, division, installed Canons, the manifest surface, the MCP server, Ollama, a manifest; each with what to do about it |
| `dmz init <template>` | write a `solution.yaml` to start from: `chatbot`, `agent`, `sdk-app`, `desk`, `logic`, `mcp` |
| `dmz validate` / `plan` / `apply` / `verify` / `destroy` | the manifest wheel, below |
| `dmz drift` / `stack` / `stacks` | what changed behind the file's back; a Stack's resources, ids and versions; the vendor's Stacks |
| `dmz keys mint` | a least-privilege key for a workspace, written straight to an env file |
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
dmz: a vendor guardrail refused the apply
  x guardrail: an approver is required (maker-checker / four-eyes)
hint: apply needs an approver distinct from the applier (four-eyes): in CI that is the merger; locally pass --approved-by <reviewer> or set DMZ_APPROVED_BY
```

### Where the key comes from

In order: `DMZAGENT_API_KEY` (or `DMZAGENT_APP_KEY`) in the environment; an
env file, either `--env-file`, `$DMZAGENT_ENV_FILE`, `./app/.env` or `./.env`
(the file `dmz apply --write-env` and `dmz keys mint` write for an
application); a profile saved with `dmz auth set` (`--profile`,
`$DMZ_PROFILE`, else `default`). `dmz whoami` says which one is in use. Subjects can be typed as the platform's MCP server
canonicalizes them: `customer:alice` becomes
`subject:<division>:customer:alice`.

## Solution Manifests

Declare the governance an application needs in one YAML file, the
platform's **Solution Manifest**. The platform validates it, computes the
Change Set against the deployed **Stack**, refuses an apply that breaks the
vendor's guardrails or names the same person as maker and checker,
provisions the resources, anchors every version on the ledger, and reports
drift. `dmz` is the client of that wheel; nothing is reconciled on this
side, and no state file lives next to the manifest.

```
dmz validate                    # schema, references, and the guardrail preview
dmz plan                        # the Change Set against the Stack; read-only
dmz apply --approved-by ravi --write-env app/.env   # reconcile; the ids land in the env file
dmz keys mint --workspace support --label site --write-env app/.env   # the app's own key
dmz verify                      # the file's expectations, on the platform's evaluator
dmz stack                       # resources, physical ids, versions with applier and approver
dmz drift [--reconcile|--adopt] # what changed on the platform; put it back, or accept it
dmz destroy --approved-by ravi  # remove what the Stack manages; workspaces and their data stay
dmz plan --markdown             # a Change Set for a pull request comment
```

A manifest (`dmz init chatbot` writes one like it; the six examples are
complete ones):

```yaml
apiVersion: dmzagent.com/v1
kind: SolutionManifest
metadata:
  name: support-bot
  vendor: ${DMZAGENT_VENDOR}                 # filled from your key
  version: 1
spec:
  divisions:
    - id: main
      division_id: ${DMZAGENT_DIVISION_ID}   # adopt the division your key belongs to
      config:
        reasoning_mode: per_frame            # reason on every message
        enforcement_posture: ${posture:-enforce}   # observe | warn | enforce

  corpora:
    - id: support-corpus                     # the Canons the rules below need;
      reasoning_canons: ["library/cn_seed_openai_agent_safety@latest"]   # installed by apply

  workspaces:
    - id: support
      workspace_id: ${DMZAGENT_WORKSPACE_ID}  # adopt the workspace your key is bound to
      division: main
      engine: reasoning
      corpus: support-corpus

  circuit_breaker_policies:                  # clauses over the subject's soul → breaker state
    - id: block-pii-leak
      workspace: support
      name: Block on PII leak
      rules: [{tag: rt_agent_pii_leak_v1, op: ">=", value: 0.6}]
      action: block

  policies:                                  # response lanes: enforce, coordinate, remediate, record
    - id: hold-scope-creep
      workspace: support
      name: Hold on scope creep
      when: [{kind: strength, label: rt_agent_scope_creep_v1, op: ">=", threshold: 0.7}]
      lane: enforce
      level: hold

  roles:                                     # the vendor guardrail requires an independent auditor
    - {principal: auditor@example.com, role: auditor, scope: main}

  expectations:                              # policy tests, run by `dmz verify`
    - id: scope-creep-is-held
      workspace: support
      labels: {rt_agent_scope_creep_v1: 0.8}
      enforce: hold
```

`${DMZAGENT_VENDOR}`, `${DMZAGENT_DIVISION_ID}` and `${DMZAGENT_WORKSPACE_ID}`
are filled from the key's own context before the file is sent, so one file
works for whoever applies it. Any other `${name:-default}` takes `--var
name=value`, so the same file is applied in `observe` posture to a staging
workspace and in `enforce` to production. References the manifest does not
know are left alone.

### What it manages

| Section | Platform resource | Notes |
| --- | --- | --- |
| `divisions` | a division and its settings (reasoning mode, posture) | `division_id` adopts an existing one |
| `workspaces` | a workspace on the reasoning or logic engine, with its corpus installed | `workspace_id` adopts the key's own |
| `corpora` | the Canons a workspace carries | `library/<canon>@<version>`, `@latest` resolved at apply; inline logic canons by id |
| `logic_canons` | a private Logic Canon with an inline rulebook | published as a new version when the rules change; the corpus and workspace re-apply |
| `circuit_breaker_policies` | a circuit-breaker policy (`allow`, `review`, `block`) | identity is the logical id; a changed workspace replaces it |
| `policies` | a response policy: `enforce`, `coordinate`, `remediate` or `record`, on the reasoning or logic soul | `config.delivery` for a webhook remediation |
| `chatbots` | a hosted chat agent and its embed | agent name, site origin, protected action, system prompt |
| `roles` | a role binding on a division | an `auditor` binding is required by the default guardrails |
| `expectations` | policy tests | not provisioned; `dmz verify` evaluates them |

Keys are not manifest resources: `dmz keys mint` mints one for a workspace
named by its logical id in the manifest and writes the secret to the env
file (mode 0600), never to the terminal unless `--show` asks.

### Stacks, versions and the two names on every apply

The platform keeps the Stack: the resources it manages with their physical
ids, and one version per apply with who applied it, who approved it and
the ledger anchor. `dmz stack` lists both. Applying an unchanged file
answers `no change`; an edited one plans as `~` (update), a resource whose
identity changed as replace, a removed one as delete, and a logic canon
whose rulebook changed as an update that re-applies its corpus and
workspace (`re-applied: ... changed`).

Every apply names a maker and a checker: the maker is the key's principal
unless `--applied-by` says otherwise, the checker is `--approved-by` or
`DMZ_APPROVED_BY`. The platform refuses an apply without a checker, or with
the same principal on both sides, and refuses a manifest without an
independent auditor or with an unpinned Canon. `dmz validate --strict` and
`dmz plan --strict` fail on those guardrails before an apply is attempted.

### The GitOps workflow

[`.github/workflows/governance.yml`](../.github/workflows/governance.yml)
turns the wheel from pull requests: a PR that edits a `solution.yaml` gets
`dmz plan --markdown` posted as a comment, and the merge runs `dmz apply
--applied-by <author> --approved-by <merger>`. The platform's maker-checker
refusal is what makes self-merging a manifest change impossible.

### Credentials and limits

`apply`, `destroy` and `drift --reconcile` need a **tenant_admin** key;
`validate`, `plan`, `verify`, `stack` and `drift` need any role in the
vendor; operating (`check`, `hold`, reviews, `mcp enforce`) needs analyst;
reads need viewer. Key revocation is a console operation. The free tier
meters requests per vendor (a short burst of ten a second, 120 a minute);
every call here is paced and a 429 is waited out.

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

The command line over HTTP against a fake platform that carries the
manifest engine (validate, plan, apply with maker-checker and the auditor
guardrail, verify, drift, destroy, key minting) and the operations surface;
the bridge in process, as a subprocess speaking standard MCP, and through
the official SDK when it is installed.
