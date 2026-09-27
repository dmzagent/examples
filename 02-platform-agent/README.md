# 02 · The platform's agent runtime, from a file on your machine

An accounts-payable agent that triages supplier invoices. It is defined in
code (`app/agent.py`), but the platform runs it: the customer supplies
**intent**, not code. A step marked `where="client"` runs here, on this
machine, against a folder the platform never sees, and only what it returns
crosses. The cloud steps are docstrings: the platform's own harness runs
them, inside the agent's allow-lists, and nothing written here executes there.

```
governance.py      the governance around the agent: settings, vocabulary,
                   breaker rules on the agent's subject, the application key
setup.sh           plan → apply → verify; installs the SDK for step 6
app/agent.py       the agent: charter, one client step, two cloud steps,
                   and the training scenarios it must decide deterministically
app/run.py         the walkthrough: deploy, train, dispatch, run, outputs, record
app/dmz_agent.py   a small client for /v1/agents, /v1/agent-jobs and the
                   runner's /v1/agent-sessions; standard library only
app/invoices/      three invoices the client step scans locally
```

## Run it

```bash
export DMZAGENT_API_KEY=ck_...       # a tenant_admin key for the workspace
./setup.sh                           # writes app/.env with an application key
python3 app/run.py                   # the six steps below
python3 app/run.py dogma             # or just print the compiled manifest
```

## What happens, step by step

1. **deploy** — `agent.compile()` produces the dogma: charter, privilege,
   allow-lists (empty here), an output schema, a disposition policy that
   holds writes for a person, and the spine `scan-inbox(client) → triage →
   draft-note`. Only instructions cross; the bodies stay on this machine.
   Pushing a changed dogma voids the grade and disarms the agent.
2. **train** — the scenarios in `agent.py` are the decisions the agent must
   make the same way every time. The platform compiles them into a decision
   table, replays each one, and grades consistency. Below **B** the arm gate
   refuses, and the gate says why in a sentence.
3. **dispatch** — deterministic auto-mode: a trained input answers from the
   table with **no model call**; an input the training never covered escalates
   to a person instead of being guessed.
4. **run** — the agent is armed and a job runs. The runner opens a session
   and *pulls* the `scan-inbox` lease (the platform never connects inward),
   runs it here, and posts back the invoice list. The cloud steps then run in
   the platform's harness. The job record lists every step with its inputs,
   outputs and cost, and halts at the first error.
5. **outputs** — the output gate: a `triage_report` file that matches its
   schema flows; one missing a required field is blocked; a write to a
   target the dogma never bound is blocked outright. A write through a bound
   connector would sit in the pending queue for a person to confirm.
6. **record** — the job is recorded on the agent stream with the SDK and the
   agent's breaker is checked. The rules in `governance.py` decide that
   state from what reasoning tags on the record.

## What the governance declares

| Declaration | Effect |
|---|---|
| `reasoning_mode="per_frame"`, `enforcement_posture` | reason on each record; `observe` first if you are turning enforcement on for the first time |
| `require_canon("cn_seed_openai_agent_safety")` | the tool-misuse and scope-creep vocabulary |
| breaker `block` on tool misuse, `review` on scope creep | the agent's own record moves its breaker |
| `coordinate/review` on escalation | a person is asked when a run keeps escalating |
| `sdk_key(...)` | the application's analyst key: it owns the agent it creates, serves client steps, records jobs, and cannot edit policy |

The agent is deployed by the application rather than by `governance.py`
because the runtime binds an agent to the key that created it, and its dogma
belongs with the code that describes it, in the same commit.

## Honest limits

- The cloud steps need the platform's inference. On a workspace without a
  model key (a local or test deployment) the job halts at `triage` with the
  error in the record; everything before it, and the output gate after it,
  still runs and is what the walkthrough prints.
- `run` holds the HTTP request while the spine walks. Start the runner
  before the job, as `run.py` does; a client step nobody serves fails after
  its `client_timeout_seconds`.
- The platform's own agent library (`dmzagent-agent`) is not published yet,
  so `app/dmz_agent.py` is a small, original client for the same endpoints.
  Swap it for the library when it ships; the shapes are the same.

## Tests

```
python3 -m unittest discover -s tests
```

The definition compiles to the manifest shape, step bodies never appear in
it, the local step reads metadata only, the runner serves a lease and reports
a local failure without its traceback, and the arm gate's blockers read as
sentences.
