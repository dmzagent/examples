"""Governance for Harbor Supply's back-office agent, which reaches the
platform through its MCP server.

The agent's model discovers the platform's tools over MCP and is told to
call enforce_covenant before it acts on a customer and record_decision
after. The verdict comes from the breaker on the customer's subject; this
file says what moves that breaker, and mints the analyst key the agent
holds: it may write to the ledger, and nothing more.

    ./setup.sh          plan → apply → verify, then the host configuration
"""
from giaas import Governance, presence, settings, strength

governance = Governance("mcp-agent", "Harbor Supply: an agent on the platform's MCP server")

governance.division_config(
    reasoning_mode="per_frame",
    enforcement_posture=settings.get("posture", "enforce"),
)

governance.require_canon("cn_seed_openai_agent_safety",
                         why="PII-leak, scope-creep and tool-misuse tags on the customer's subject")

# What stops an action outright, what asks a person, what pauses.
governance.breaker_policy("Block on PII leak",
                          rules=[("rt_agent_pii_leak_v1", ">=", 0.6)], action="block",
                          description="an export that would leak is a hard stop")
governance.breaker_policy("Review on scope creep",
                          rules=[("rt_agent_scope_creep_v1", ">=", 0.5)], action="review")
governance.policy("Hold on tool misuse",
                  when=[strength("rt_agent_tool_misuse_v1", ">=", 0.5)], lane="enforce", level="hold")
governance.policy("Ask a person about tool misuse",
                  when=[presence("rt_agent_tool_misuse_v1")], lane="coordinate", level="review")

# The agent's key. Minted once, written to app/.env, analyst: enforce and
# record are allowed; changing configuration is not.
governance.sdk_key("mcp agent", env_var="DMZAGENT_APP_KEY")
governance.env("AGENT_NAME", "Harbor Supply back office")

governance.expect("tool misuse holds and asks", labels={"rt_agent_tool_misuse_v1": 0.7},
                  enforce="hold", coordinate="review")
governance.expect("a leak is a hard stop", labels={"rt_agent_pii_leak_v1": 0.8},
                  policies=["Block on PII leak"])
