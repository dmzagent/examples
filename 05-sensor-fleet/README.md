# 05 · A sensor fleet governed by the deterministic logic engine

No chatbot, no model. Harbor Supply's chiller pumps report temperature and
pressure; a **rulebook** of predicates and half-life accumulators reads every
event on the platform's logic engine, keeps a *logic soul* per pump (labels
whose strength grows with each match and decays over time), and policies on
that soul move each pump's circuit breaker. The fleet controller checks the
breaker before it lets a pump's actuator run.

This is the same platform, the same breaker, the same ledger and review queue
as the chatbot examples, pointed at a system that acts without ever being
asked a question. The rulebook is a Logic Canon declared inline in
`solution.yaml`; change a threshold, apply, and the plan shows the canon
being updated and the corpus and workspace re-applied with the new version.

```
solution.yaml    the Solution Manifest: the rulebook (four rules, two with
                 accumulators and bands) as an inline Logic Canon, the corpus
                 that pins it, the logic workspace, the policies on the logic
                 soul, posture, the auditor role, the expectations
setup.sh         validate → plan → apply → mint the controller's key →
                 verify; installs the SDK
app/fleet.py     the controller: telemetry in, breaker check before each
                 actuator tick, logic souls at the end
```

## Run it

This example needs a **logic** workspace: when you create the workspace in
the console, choose the logic engine. Mint a tenant_admin key for it, then:

```bash
export DMZAGENT_API_KEY=ck_...
export DMZ_APPROVED_BY=reviewer             # who approved the change (maker-checker); in CI, the merger
./setup.sh                                  # publishes the rulebook, installs it, writes app/.env
python3 app/fleet.py                        # the scripted day
python3 app/fleet.py --pump pump-7 --temp 92 --ticks 8   # drive one pump by hand
python3 app/fleet.py --run tue              # replay the day on fresh subjects
```

Everything here works on a deployment without inference: the logic engine is
deterministic and evaluates inline. The run prints one line per pump per
tick: the reading, a `logic` column, the breaker state, and what the
actuator did. The `logic` column is what fired on this reading (a stateless
rule such as `sensor-offline`) and, for an accumulator, the band the pump's
pattern now sits in (`over-temp→enforce`). A band outlives the reading that
reached it: the accumulator decays on its half-life, so a pump that
overheated keeps reporting its band on cool readings until the stress has
decayed below the lowest band.

The stress and the breaker live on the platform, not in this process. Run
the scripted day again within the half-life and pump-7 starts held, with
its soul still hot; that is the platform remembering what the controller
cannot. `--run <tag>` suffixes every pump name so a replay starts on fresh
subjects.

## What the rulebook says

| Rule | Match | Then |
|---|---|---|
| `over-temp` | `temp_c > 85` | accumulate weight 1, half-life 10 min; bands record ≥1, enforce ≥3 (hold), human ≥5 (review) |
| `pressure-spike` | `pressure_bar > 9` | accumulate weight 2, half-life 5 min; bands record ≥2, enforce ≥4 (block) |
| `sensor-offline` | `status in [offline, fault]` | coordinate: review |
| `bad-reading` | `temp_c < -40` or `> 200` | record only: data trouble is not thermal trouble |

Half-lives are the point. One hot reading is recorded and forgotten. Three
inside ten minutes hold the pump; five ask a person; the policy in
`solution.yaml` blocks it outright at six. Reinforcement climbs, quiet decays.

On install, the platform translates the rulebook's inline dispositions into
policies on the logic soul, one per band, so every response goes through the
same policy engine the reasoning examples use. `solution.yaml` adds two the
rulebook does not name: the hard stop at strength 6 and a review for an
offline sensor.

## What the manifest declares

| Section of `solution.yaml` | Effect |
|---|---|
| `logic_canons[chiller]` with an inline `rulebook` | a private Logic Canon: published as version 1 by the first apply, republished as version N+1 when the rules change |
| `corpora[fleet-corpus].logic_canons: [chiller]` | the corpus pins the inline canon; a new version re-applies the corpus and the workspace, so the workspace always carries the rules in the file |
| `workspaces[fleet]` with `engine: logic` | the adopted workspace runs the logic engine; apply sets the engine kind if it differs |
| `policies` with `soul: logic` | responses on accumulated labels: block at strength 6, review an offline sensor |
| `divisions[main].config.enforcement_posture` | `observe` to watch the rate first, `enforce` when ready |
| `roles`: an auditor on the division | the segregation of duties the vendor guardrail requires |
| `expectations` with `soul: logic` | what the engine must resolve at strengths 1, 4 and 6; `dmz verify` runs them |

The controller's key is minted by `setup.sh` with `dmz keys mint` (analyst:
it emits events and checks breakers) and written to `app/.env`.

Edit a threshold and run `./setup.sh` again to see the versioning:

```
Plan: 0 to add, 3 to change, 0 to replace, 0 to remove, 4 unchanged
  ~ logic_canon              chiller  rulebook
  ~ corpus                   fleet-corpus  re-applied: chiller changed
  ~ workspace                fleet  re-applied: fleet-corpus changed
```

## Honest limits

- The controller emits through `POST /v1/logic/events` directly; the SDK has
  no method for the logic door yet, and `check()` is the SDK.
- Releasing a held pump is a person's act: the console's review queue, or
  `POST /v1/cb/release` as the desk in example 04 does.
- Only a logic workspace evaluates rulebooks. The manifest adopts the
  workspace the key is bound to and declares `engine: logic`; apply switches
  the engine of a reasoning workspace rather than refusing, so point this
  example at a workspace made for it.

## Tests

```
pip install -r requirements.txt
python3 -m unittest discover -s tests
```

The rulebook's shape, and the controller against a fake logic door: a hot
pump climbs from record to enforce and is held; a cool pump keeps running;
an offline sensor fires its rule; a rate-limited breaker check is waited out
once; a check that cannot be made fails closed and the actuator stays off.
