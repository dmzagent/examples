"""Governance for Harbor Supply's back-office automations, and the desk that
operates them.

Three agents act on the company's behalf: a payments agent that settles
supplier invoices, a records agent that exports customer data, and a support
agent. This file declares how each kind of trouble is answered:

  * a hard stop for what cannot be undone (a personal-data leak);
  * a recoverable hold plus a review for what a person should look at
    (tool misuse), so an agent pauses instead of failing and an operator
    releases it from the desk;
  * an escalation for deception;
  * a remediation directive delivered to the company's own endpoint.

The enforcement posture is a variable so the same file runs in `observe`
on a new workspace (decisions recorded, nothing stopped) and in `enforce`
once the rate of holds is understood: ./setup.sh --var posture=observe
"""
from giaas import Governance, settings, strength

governance = Governance("review-desk", "hold, review and release for Harbor Supply's agents")

governance.division_config(
    reasoning_mode="per_frame",
    enforcement_posture=settings.get("posture", "enforce"),
)

governance.require_canon(
    "cn_seed_openai_agent_safety",
    why="PII-leak, tool-misuse, scope-creep and deception tags on what the agents do",
)

# --- What cannot be undone is blocked outright ------------------------------
governance.breaker_policy(
    "Block on PII leak",
    rules=[("rt_agent_pii_leak_v1", ">=", 0.5)],
    action="block",
    description="Personal data that has left cannot be recalled; the breaker opens and stays open until an operator releases it.",
)

# --- What a person should look at is held, and queued ---------------------
governance.policy(
    "Hold on tool misuse",
    when=[strength("rt_agent_tool_misuse_v1", ">=", 0.5)],
    lane="enforce",
    level="hold",
    description="A recoverable pause: guard() refuses, the desk shows why, an operator releases.",
)
governance.policy(
    "Review tool misuse",
    when=[strength("rt_agent_tool_misuse_v1", ">=", 0.5)],
    lane="coordinate",
    level="review",
)
governance.breaker_policy(
    "Flag scope creep",
    rules=[("rt_agent_scope_creep_v1", ">=", 0.5)],
    action="review",
    description="Half-open: the agent keeps working, every check says warning=True, the desk shows it.",
)

# --- Deception is escalated to the people who own the agent ---------------
governance.policy(
    "Escalate deception",
    when=[strength("rt_agent_deception_v1", ">=", 0.6)],
    lane="coordinate",
    level="escalate",
)

# --- And a leak triggers the company's own remediation playbook ------------
# The platform emits a directive to the workspace's registered webhooks and,
# when a delivery target is declared here, to that endpoint too. Live
# outbound delivery is armed by the platform operator; until then every
# attempt is recorded as `not_armed` on the platform, which is honest.
governance.policy(
    "Remediate a PII leak",
    when=[strength("rt_agent_pii_leak_v1", ">=", 0.5)],
    lane="remediate",
    level="webhook",
    config={"delivery": {"endpoint": settings.get("remediation_url", "http://localhost:8002/hooks/remediate"),
                         "method": "POST"}},
    description="Tell the company's own systems to start the leak playbook.",
)

# The desk and the simulator share one analyst key: it reads states, decisions
# and the review queue, claims and resolves reviews, holds and releases
# subjects, and emits the agents' events. It cannot edit any policy above.
governance.sdk_key("review-desk", env_var="DMZAGENT_APP_KEY")

governance.expect(
    "a leak blocks and remediates",
    labels={"rt_agent_pii_leak_v1": 0.7},
    enforce="block", remediate="webhook", policies=["Block on PII leak", "Remediate a PII leak"],
)
governance.expect(
    "tool misuse holds and asks",
    labels={"rt_agent_tool_misuse_v1": 0.6}, fired=["rt_agent_tool_misuse_v1"],
    enforce="hold", coordinate="review", policies=["Hold on tool misuse", "Review tool misuse"],
)
governance.expect(
    "scope creep only flags",
    labels={"rt_agent_scope_creep_v1": 0.6},
    enforce="challenge", coordinate=None,
)
governance.expect(
    "deception escalates",
    labels={"rt_agent_deception_v1": 0.8},
    coordinate="escalate",
)
