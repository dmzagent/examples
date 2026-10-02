# 08 · A fleet of Claude Code sessions, governed

Dream Builder runs one `claude -p` session per lease, each in its own git
worktree. Its runner hears every tool call before the call happens, through
the `PreToolUse` hook, and after it, through `PostToolUse`. It also hears
every stream event. The runner turns each of these into one event for DMZ
Agent. The rulebook in this manifest decides, and the runner's hook enforces
the answer.

| Kind | Rules | The engine does |
|---|---|---|
| hard rails | a push from a session; a write outside its worktree; any call after its lease lapsed; a session running `mem approve` (consenting to its own claim); an unpinned model | **block** |
| pauses | budget spent; a credential or billing failure; a settled minute where metered spend is > 1% off the bill | **hold**. The spend hold also stops the scheduler growing the fleet |
| questions | a stalled session; two live leases on one path; a minute the bill has not confirmed after 15 minutes | **review** |
| a record | an edit the session's plan never named (a sin) | recorded; at strength 5 inside the hour, **escalate** |

The division starts in `observe`: at N=1 every rule records. It moves to
`enforce` once the operator's first audit passes. The thresholds are the
operator's: spend within 1%, live within 75 s.

```
solution.yaml      the `fleet-rails` rulebook (12 rules), the rails and transcripts
                   workspaces, 16 policies, roles, outputs, 10 expectations
tests/events.json  runner events and the tags each must produce, including the
                   look-alikes that must produce none (`pushd`, `mem promote`,
                   a rate-limit retry, a bill 0.8% off)
```

The runner side is step 1c of `docs/32-piloting-claude-code.md` in
`agent-memory-dream-maker`.

## What `apply` builds today, and what was checked

The same split as `07-loan-origination`:

- **Built:** the rulebook, workspaces and policies are provisioned.
- **Recorded only:** sources, roles and outputs are recorded in the Stack.

The manifest went through the platform's own validator, rulebook compiler,
guardrails and policy engine, offline, and all checks passed. Each check was
then broken on purpose, and a named failure caught all nine breaks:

- the push regex loosened;
- the worktree check dropped;
- the self-consent regex narrowed;
- the divergence floor moved to 2%;
- the divergence hold downgraded;
- a rate limit treated as a failure;
- a collision escalated to a block;
- the sin threshold lowered;
- the auditor made the operator.
