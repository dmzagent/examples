"""Governance for the invoice-triage agent.

The agent itself is defined and deployed by the application (app/agent.py):
the platform binds an agent to the key that created it, and its dogma is
versioned with the code that describes it. What this file declares is the
governance around it — the settings, the vocabulary, the breaker on the
agent's subject, and the least-privilege key the application runs under.
"""
from giaas import Governance, settings, strength

governance = Governance("platform-agent", "governance around the invoice-triage agent")

governance.division_config(
    reasoning_mode="per_frame",
    enforcement_posture=settings.get("posture", "enforce"),
)

governance.require_canon(
    "cn_seed_openai_agent_safety",
    why="tool-misuse and scope-creep tags on what the agent does",
)

# The agent records each job on the agent stream (app/run.py, step 6). These
# rules evaluate the agent's soul built from that record and set the breaker
# the application checks before it acts on a job's outputs.
governance.breaker_policy(
    "Block on tool misuse",
    rules=[("rt_agent_tool_misuse_v1", ">=", 0.6)],
    action="block",
    description="The agent used a capability in a way its charter does not cover.",
)
governance.breaker_policy(
    "Review on scope creep",
    rules=[("rt_agent_scope_creep_v1", ">=", 0.5)],
    action="review",
    description="The agent is drifting beyond invoice triage (half-open: allowed, flagged).",
)
governance.policy(
    "Review a run that keeps escalating",
    when=[strength("rt_pattern_escalation_v1", ">=", 0.5)],
    lane="coordinate",
    level="review",
)

# The application's key: it creates and runs the agent, serves client steps,
# and records jobs. It cannot change any policy above.
governance.sdk_key("invoice-triage app", env_var="DMZAGENT_APP_KEY")

governance.expect(
    "tool misuse opens the breaker",
    labels={"rt_agent_tool_misuse_v1": 0.7}, enforce="block", policies=["Block on tool misuse"],
)
governance.expect(
    "scope creep is flagged, not stopped",
    labels={"rt_agent_scope_creep_v1": 0.6}, enforce="challenge", policies=["Review on scope creep"],
)
