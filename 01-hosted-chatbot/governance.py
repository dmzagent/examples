"""Governance for Harbor Supply's hosted support chat agent.

The chat agent itself runs on the platform: the platform serves the widget,
answers each turn, and — before it answers — feeds the visitor's message
through its reasoning pipeline and checks the agent's own circuit breaker.
This file declares what that breaker and the surrounding lanes should do.

Apply it with `./setup.sh` (or `python -m giaas apply governance.py`).
"""
from giaas import Governance, presence, settings, strength

governance = Governance(
    "hosted-chatbot",
    "Harbor Supply's support chat agent, hosted by the platform and governed here",
)

# Reason on every message rather than when a chat session closes, so the
# panel next to the widget shows decisions while you are still typing. This
# is the metered per-frame mode; per_trace is the cheaper default.
# `posture` lets the same file run in observe mode on a staging workspace:
#   ./setup.sh --var posture=observe
governance.division_config(
    reasoning_mode="per_frame",
    enforcement_posture=settings.get("posture", "enforce"),
)

# The agent-safety Canon supplies the tags below (deception, PII leak, scope
# creep, tool misuse). A tag the workspace's vocabulary does not contain is a
# policy that never fires, so this is checked before anything is applied.
governance.require_canon(
    "cn_seed_openai_agent_safety",
    why="tags the agent's own replies for deception, PII leaks and scope creep",
)

# --- The breaker: what stops the agent ------------------------------------
# These evaluate the AGENT's soul — what the bot itself has been saying. The
# platform checks the agent's breaker before every reply; `block` opens it,
# so the widget answers "paused" until a person releases it.
governance.breaker_policy(
    "Block on deceptive output",
    rules=[("rt_agent_deception_v1", ">=", 0.6)],
    action="block",
    description="The agent stated something it could not know or contradicted the record.",
)
governance.breaker_policy(
    "Block on PII leak",
    rules=[("rt_agent_pii_leak_v1", ">=", 0.5)],
    action="block",
    description="The agent disclosed personal data it had no business repeating.",
)
governance.breaker_policy(
    "Review on scope creep",
    rules=[("rt_agent_scope_creep_v1", ">=", 0.5)],
    action="review",
    description="The agent drifted outside support (half-open: allowed, but flagged).",
)

# --- The lanes: what happens around the breaker ---------------------------
# A conversation that keeps escalating opens a review for a person. This tag
# is in the core vocabulary every workspace has, so it works before any Canon
# is installed.
governance.policy(
    "Review an escalating conversation",
    when=[strength("rt_pattern_escalation_v1", ">=", 0.5)],
    lane="coordinate",
    level="review",
    description="Severity or intensity is rising across recent turns; a person should look.",
)
# Scope creep is recoverable: hold the agent (allow=false, but releasable
# from the review desk) and open the review that lets someone release it.
governance.policy(
    "Hold the agent on scope creep",
    when=[strength("rt_agent_scope_creep_v1", ">=", 0.7)],
    lane="enforce",
    level="hold",
)
governance.policy(
    "Open a review on scope creep",
    when=[presence("rt_agent_scope_creep_v1")],
    lane="coordinate",
    level="review",
)

# --- The chat agent ---------------------------------------------------------
# The platform only answers the widget from this site. `localhost` is where
# app/serve.py runs; pass --var site_domain=support.example.com for a real one.
bot = governance.chatbot(
    "Harbor Supply support",
    site_domain=settings.get("site_domain", "localhost"),
    protected_action="refund.issue",
    system_prompt=(
        "You are the support assistant for Harbor Supply, a marine hardware "
        "store. Help with orders, returns and product questions. You cannot "
        "issue refunds yourself; a person on the team does that. Never repeat "
        "a customer's card number, address or phone number back to them."
    ),
)

# A least-privilege key for the site server: it reads breaker state, decisions
# and reviews for the panel, and can hold or release the agent. It never
# manages policies.
governance.sdk_key("hosted-chatbot site", env_var="DMZAGENT_APP_KEY")

# Outputs the site server needs, written to app/.env by setup.sh.
governance.env("CHATBOT_EMBED_ID", bot.ref("embed_id"))
governance.env("CHATBOT_AGENT_SUBJECT_ID", bot.ref("agent_subject_id"))
governance.env("CHATBOT_EMBED_SCRIPT_URL", bot.ref("embed_script_url"))
governance.env("CHATBOT_EMBED_CHAT_URL", bot.ref("embed_chat_url"))
governance.env("CHATBOT_AGENT_NAME", bot.name)

# --- Policy tests, run by the platform's own evaluator (giaas verify) ------
governance.expect(
    "a leaked card number opens the breaker",
    labels={"rt_agent_pii_leak_v1": 0.8},
    enforce="block", policies=["Block on PII leak"],
)
governance.expect(
    "scope creep is held, not blocked, and reviewed",
    labels={"rt_agent_scope_creep_v1": 0.75}, fired=["rt_agent_scope_creep_v1"],
    enforce="hold", coordinate="review",
    policies=["Hold the agent on scope creep", "Open a review on scope creep"],
)
governance.expect(
    "an ordinary help request changes nothing",
    labels={"rt_signal_help_request_v1": 0.9},
    enforce=None, coordinate=None,
)
