"""The governance files ``dmz init`` writes. Each is a working starting point
for one shape of application; the comments say what to change first."""
from __future__ import annotations

from string import Template

TEMPLATES: dict[str, tuple[str, str]] = {}


def _register(name: str, summary: str, body: str) -> None:
    TEMPLATES[name] = (summary, body)


def render(name: str, *, project: str) -> str:
    summary, body = TEMPLATES[name]
    return Template(body).substitute(project=project, slug=project.lower().replace(" ", "-"))


_register("chatbot", "the platform's hosted chat agent on your site", '''\
"""Governance for $project: the platform's hosted chat agent on a website.

    dmz plan        # what would change
    dmz apply       # reconcile; ids and the site key land in .env
    dmz verify      # requirements met? policies resolve as declared?
"""
from giaas import Governance, presence, settings, strength

governance = Governance("$slug", "$project: hosted chat agent")

# Reason on every message; start in `observe` until the decisions look right.
governance.division_config(
    reasoning_mode="per_frame",
    enforcement_posture=settings.get("posture", "observe"),   # observe | warn | enforce
)

# The agent-safety vocabulary: prompt injection, PII leak, tool misuse, scope creep.
# API keys cannot install a Canon; `dmz apply` reports it if the console has not.
governance.require_canon("cn_seed_openai_agent_safety",
                         why="the tags the breaker rules below are written against")

# Breaker rules on the agent's own subject: what stops the agent outright.
governance.breaker_policy("Block on PII leak",
                          rules=[("rt_agent_pii_leak_v1", ">=", 0.6)], action="block")
governance.breaker_policy("Review on scope creep",
                          rules=[("rt_agent_scope_creep_v1", ">=", 0.5)], action="review")

# Response lanes: a recoverable hold plus a review for a person.
governance.policy("Hold on scope creep",
                  when=[strength("rt_agent_scope_creep_v1", ">=", 0.7)], lane="enforce", level="hold")
governance.policy("Ask a person about scope creep",
                  when=[presence("rt_agent_scope_creep_v1")], lane="coordinate", level="review")

# The hosted agent itself. The widget is served by the platform; the site
# only embeds it. `protected_action` is what a held conversation cannot do.
governance.chatbot("$project support",
                   site_domain=settings.get("site_domain", "localhost"),
                   protected_action="refund.issue",
                   system_prompt="You are the $project support assistant. Be brief and kind.")

# A least-privilege key for the site server, minted once and written to .env.
governance.sdk_key("$slug site", env_var="DMZAGENT_APP_KEY")

# Policy tests, run by the platform's own evaluator on `dmz verify`.
governance.expect("scope creep is held and reviewed",
                  labels={"rt_agent_scope_creep_v1": 0.8}, enforce="hold", coordinate="review")
''')

_register("agent", "an agent on the platform's runtime, defined in code", '''\
"""Governance for $project: an agent the platform runs from your definition.

    dmz plan && dmz apply --write-env app/.env && dmz verify
"""
from giaas import Governance, presence, settings, strength

governance = Governance("$slug", "$project: platform agent")

governance.division_config(reasoning_mode="per_frame",
                           enforcement_posture=settings.get("posture", "observe"))
governance.require_canon("cn_seed_openai_agent_safety",
                         why="tool misuse and scope creep tags on the agent's subject")

# Tool misuse stops the agent; scope creep asks a person before it continues.
governance.breaker_policy("Block on tool misuse",
                          rules=[("rt_agent_tool_misuse_v1", ">=", 0.6)], action="block")
governance.breaker_policy("Review on scope creep",
                          rules=[("rt_agent_scope_creep_v1", ">=", 0.5)], action="review")
governance.policy("Ask a person about an escalation",
                  when=[presence("rt_pattern_escalation_v1")], lane="coordinate", level="review")

# The key the local runner uses to deploy, train, arm and run the agent.
governance.sdk_key("$slug runner", env_var="DMZAGENT_APP_KEY")

governance.expect("tool misuse is blocked", labels={"rt_agent_tool_misuse_v1": 0.9},
                  policies=["Block on tool misuse"])
''')

_register("sdk-app", "your own application with the SDK embedded", '''\
"""Governance for $project: an application that embeds the SDK.

Every turn is recorded; before a sensitive tool runs, the app checks the
breaker on its subject. This file declares what moves that breaker.

    dmz plan && dmz apply --write-env app/.env && dmz verify
"""
from giaas import Governance, presence, settings, strength

governance = Governance("$slug", "$project: SDK-governed application")

governance.division_config(reasoning_mode="per_frame",
                           enforcement_posture=settings.get("posture", "observe"))

# The OWASP LLM Top 10 vocabulary is free and covers the usual trouble.
governance.require_canon("cn_owasp_llm_top10", why="prompt injection, disclosure, excessive agency")

INJECTION = "rt_owasp_llm01_prompt_injection_2025_v1"
DISCLOSURE = "rt_owasp_llm02_sensitive_information_disclosure_2025_v1"
AGENCY = "rt_owasp_llm06_excessive_agency_2025_v1"

governance.breaker_policy("Block on disclosure", rules=[(DISCLOSURE, ">=", 0.5)], action="block")
governance.breaker_policy("Review on injection", rules=[(INJECTION, ">=", 0.5)], action="review")
governance.policy("Hold on strong injection", when=[strength(INJECTION, ">=", 0.75)],
                  lane="enforce", level="hold")
governance.policy("Escalate excessive agency", when=[strength(AGENCY, ">=", 0.5)],
                  lane="coordinate", level="escalate")

governance.sdk_key("$slug app", env_var="DMZAGENT_APP_KEY")
governance.env("BOT_SUBJECT_SLUG", "$slug-assistant")

governance.expect("strong injection is held", labels={INJECTION: 0.9}, enforce="hold")
governance.expect("disclosure trips the breaker", labels={DISCLOSURE: 0.6},
                  policies=["Block on disclosure"])
''')

_register("desk", "an operations desk: hold, review, release", '''\
"""Governance for $project: what each kind of trouble does, and who decides.

A hard stop for a leak; a recoverable hold plus a review for tool misuse;
an escalation for deception; a webhook when remediation is due.
"""
from giaas import Governance, presence, settings, strength

governance = Governance("$slug", "$project: operations desk")

governance.division_config(enforcement_posture=settings.get("posture", "enforce"))
governance.require_canon("cn_seed_openai_agent_safety", why="the agent-safety tags below")

governance.breaker_policy("Block on PII leak", rules=[("rt_agent_pii_leak_v1", ">=", 0.6)], action="block")
governance.policy("Hold on tool misuse", when=[strength("rt_agent_tool_misuse_v1", ">=", 0.5)],
                  lane="enforce", level="hold")
governance.policy("Review tool misuse", when=[presence("rt_agent_tool_misuse_v1")],
                  lane="coordinate", level="review")
governance.policy("Escalate deception", when=[strength("rt_agent_deception_v1", ">=", 0.6)],
                  lane="coordinate", level="escalate")
governance.policy("Notify remediation", when=[strength("rt_agent_pii_leak_v1", ">=", 0.6)],
                  lane="remediate", level="webhook",
                  config={"delivery": {"endpoint": settings.get("remediation_url",
                                                                 "http://localhost:8002/hooks/remediate"),
                                       "method": "POST"}})

governance.sdk_key("$slug desk", env_var="DMZAGENT_APP_KEY")

governance.expect("tool misuse holds and asks", labels={"rt_agent_tool_misuse_v1": 0.7},
                  enforce="hold", coordinate="review")
governance.expect("a leak is a hard stop", labels={"rt_agent_pii_leak_v1": 0.8},
                  policies=["Block on PII leak"])
''')

_register("logic", "a rulebook on the deterministic logic engine (logic workspace)", '''\
"""Governance for $project: a rulebook on the logic engine. No model.

Needs a LOGIC workspace. Rules are predicates over event fields; an
`accumulate` block turns repeated matches into a decaying strength with
bands that escalate record -> enforce -> human.
"""
from giaas import Governance, presence, settings, strength

governance = Governance("$slug", "$project: logic rulebook")

SLUG = "$slug"
RULEBOOK = {
    "rules": [
        {
            "id": "over-limit",
            "match": {"predicate": {"kind": "comparison", "field": "value", "op": "gt", "value": 85}},
            "accumulate": {"weight": 1, "half_life": "10m",
                           "bands": {"record": 1, "enforce": 3, "human": 5}},
            "on_true": [{"disposition": "enforce", "level": "hold", "tag": "over_limit"}],
        },
        {
            "id": "offline",
            "match": {"predicate": {"kind": "value", "field": "status", "op": "in", "value": ["offline", "fault"]}},
            "on_true": [{"disposition": "coordinate", "level": "review", "tag": "offline"}],
        },
    ],
}

governance.division_config(enforcement_posture=settings.get("posture", "enforce"))
governance.logic_rulebook("$project rulebook", slug=SLUG, rulebook=RULEBOOK,
                          description="thresholds and half-lives for $project")

# Policies on the logic soul: the hard stop the rulebook does not name.
governance.policy("Block on sustained over-limit", when=[strength("over_limit", ">=", 6)],
                  lane="enforce", level="block", soul="logic")
governance.policy("Review an offline sensor", when=[presence("offline")],
                  lane="coordinate", level="review", soul="logic")

governance.sdk_key("$slug controller", env_var="DMZAGENT_APP_KEY")
governance.env("RULEBOOK_SLUG", SLUG)

governance.expect("one match is recorded", labels={f"{SLUG}@1:over-limit": 1}, soul="logic")
governance.expect("sustained over-limit blocks", labels={"over_limit": 6}, soul="logic", enforce="block")
''')

_register("mcp", "an agent on the platform's MCP server", '''\
"""Governance for $project: an agent that reaches the platform over MCP.

The agent calls enforce_covenant before it acts and record_decision after.
The verdict comes from the breaker on the subject; this file says what
moves that breaker and which key the agent holds (analyst: it may write
to the ledger, and nothing more).
"""
from giaas import Governance, presence, settings, strength

governance = Governance("$slug", "$project: MCP agent")

governance.division_config(reasoning_mode="per_frame",
                           enforcement_posture=settings.get("posture", "enforce"))
governance.require_canon("cn_seed_openai_agent_safety", why="the agent-safety tags below")

governance.breaker_policy("Block on PII leak", rules=[("rt_agent_pii_leak_v1", ">=", 0.6)], action="block")
governance.breaker_policy("Review on scope creep", rules=[("rt_agent_scope_creep_v1", ">=", 0.5)],
                          action="review")
governance.policy("Hold on tool misuse", when=[strength("rt_agent_tool_misuse_v1", ">=", 0.5)],
                  lane="enforce", level="hold")
governance.policy("Ask a person about tool misuse", when=[presence("rt_agent_tool_misuse_v1")],
                  lane="coordinate", level="review")

governance.sdk_key("$slug agent", env_var="DMZAGENT_APP_KEY")

governance.expect("tool misuse holds and asks", labels={"rt_agent_tool_misuse_v1": 0.7},
                  enforce="hold", coordinate="review")
''')
