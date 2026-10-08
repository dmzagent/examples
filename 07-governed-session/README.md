# 07 · A governed session: an agent that asks before every call

Harbor Supply runs a coding agent on its billing service. Before each tool
call the agent asks DMZAgent, and the answer is a directive: **proceed**,
**warn**, **hold**, **block** or **shutdown**. The agent runs the call only
on proceed or warn. A hold waits on an approval a person decides, and a
call that did not run is reported back with who refused it. Every call the
agent made, ran or was refused becomes the seat's conduct record, positive
behaviors as well as negative ones.

This is agent mode (SDK spec 0.11.0, §1.9 and §2.11–§2.13): one step at a
time, with the governor's word taken as given.

```
solution.yaml     a logic rulebook: a push is blocked, curl and wget are
                  held for a person, a test run is recorded as a positive
                  behavior; the posture as a variable
setup.sh          validate → plan → apply → mint the app's key → verify;
                  installs the SDK
app/agent.py      the agent's session: an intent, then each call asked
                  before it runs, its result or refusal reported after
app/approve.py    the operator: what is waiting, approve or decline it with
                  a name, and the seat's conduct record
```

## Run it

```bash
export DMZAGENT_API_KEY=ck_...           # a tenant_admin key for the workspace
export DMZ_APPROVED_BY=reviewer          # who approved the change (maker-checker)
./setup.sh
python3 app/agent.py                     # the session; it waits on the held call
python3 app/approve.py                   # in another terminal: what is waiting
python3 app/approve.py apr_… approve --actor dana
python3 app/approve.py --behaviors       # the conduct record
```

What the agent prints, with the hold approved:

```
session sess_4b1e9c02: Fix the invoice rounding test
  c1 Bash: pytest -q tests/test_invoice.py
     observed: verified_before_claiming (positive)
     proceed: ran → 1 failed: 412.505 rounded to 412.50
  c2 Edit: billing/rounding.py
     proceed: ran → edited
  c3 Bash: rm -rf /srv/harbor/ledger
     refused by this harness: deletes outside the repository
  c4 Bash: curl -sS https://pypi.org/pypi/decimal-tools/json
     held for a person: approval apr_… (unsanctioned egress)
     decide it with:  python3 app/approve.py apr_… approve
     approval approved by dana
     hold: ran → {"info": {...}}
  c5 Bash: git push origin HEAD
     observed: remote_write (negative)
     block: not run
```

## What the session does

| Call | The rulebook | The directive | The agent |
|---|---|---|---|
| `pytest` | `verified` records a positive behavior | proceed | runs it, reports `ok` |
| an edit | nothing matches | proceed | runs it, reports `ok` |
| `rm -rf` outside the repo | (never asked) | — | its own harness refuses it and reports `refused_by: harness` |
| `curl` | `egress` holds | hold, naming an approval | waits on `get_approval()`; runs it only if approved |
| `git push` | `remote-write` blocks | block | does not run it, reports `refused_by: governor`, and carries on |

Three things in `agent.py` are the point of the example:

- **Branch on `r.runs`, never on the directive's word.** `runs` is true for
  proceed and warn only, so a directive this SDK does not know is read as a
  block rather than a yes.
- **A hold waits on its own approval.** Approved runs. Declined, expired, or
  still pending when the agent stops waiting is a block. A hold that names no
  approval is a block too, since nobody could answer it.
- **Report what did not run.** A refusal names who refused: `governor` for
  DMZAgent's directive, `harness` for the runner's own rules. A rule an agent
  got around is only recognisable against the refusal it got around.

## What the operator does

`approve.py` lists what waits: the call the agent was about to make,
verbatim, and the words of the rule that held it. It decides with a
person's name (`--actor`, default your login). The API key identifies this
integration, and an approval decided by the integration that asked for it
has recorded nobody. The first decision wins: a second one, or one after
the deadline, is answered with the approval as it stands.

## Honest limits

- The tools are simulated: the agent prints what it would have run. Swap
  `CALLS` for a model's tool calls and the loop is unchanged.
- The rules match commands by prefix. `bash -c "curl …"` gets past them; a
  real deployment pairs this rulebook with a sandbox, and reports the
  sandbox's refusals as `refused_by: host`.
- An approval left undecided expires into a decline after the division's
  hold window (one hour by default).

## Tests

```
pip install -r requirements.txt
python3 -m unittest discover -s tests
```

The agent and the operator against a fake platform that answers the way the
rulebook does:
- the intent comes first;
- ordinary calls run and report;
- a block is refused and the session carries on;
- the harness refuses without asking;
- a held call runs only when approved, and a decline or no decision refuses it;
- a hold with no approval is a block;
- a second decision is answered with the first;
- the conduct record reads back.

Breaking any of the agent's guards on purpose fails a named test.
