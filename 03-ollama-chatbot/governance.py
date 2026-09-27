"""Governance for Harbor Supply's own assistant — a chatbot the company runs
itself on an open-weights model through Ollama, with the DMZAgent SDK
embedded in it.

The application (app/chat.py) sends every turn to the platform and checks
the assistant's circuit breaker before it runs a sensitive tool. This file
declares what the platform does with those turns: the vocabulary it tags
them with, and the rules that move the breaker or ask a person.
"""
from giaas import Governance, presence, settings, strength

# The OWASP LLM Top 10 (2025) as warning labels. Free, in the Library.
INJECTION = "rt_owasp_llm01_prompt_injection_2025_v1"
DISCLOSURE = "rt_owasp_llm02_sensitive_information_disclosure_2025_v1"
OUTPUT_HANDLING = "rt_owasp_llm05_improper_output_handling_2025_v1"
EXCESSIVE_AGENCY = "rt_owasp_llm06_excessive_agency_2025_v1"

governance = Governance("ollama-chatbot", "Harbor Supply's Ollama assistant, governed by the SDK")

governance.division_config(
    reasoning_mode="per_frame",
    enforcement_posture=settings.get("posture", "enforce"),
)

governance.require_canon(
    "cn_owasp_llm_top10",
    why="labels each turn with the OWASP LLM Top 10: injection, disclosure, excessive agency",
)

# --- The breaker (what stops the assistant from acting) --------------------
governance.breaker_policy(
    "Block on sensitive information disclosure",
    rules=[(DISCLOSURE, ">=", 0.5)],
    action="block",
    description="Disclosure is not recoverable once it has left, so this one blocks rather than holds.",
)
governance.breaker_policy(
    "Flag suspected prompt injection",
    rules=[(INJECTION, ">=", 0.5)],
    action="review",
    description="Half-open: the assistant may still act, but every check says warning=True.",
)

# --- The lanes (what happens around the breaker) ---------------------------
governance.policy(
    "Hold the assistant on strong prompt injection",
    when=[strength(INJECTION, ">=", 0.75)],
    lane="enforce",
    level="hold",
    description="A hold costs a review, not a failed request; a person releases it.",
)
governance.policy(
    "Escalate excessive agency",
    when=[strength(EXCESSIVE_AGENCY, ">=", 0.5)],
    lane="coordinate",
    level="escalate",
    description="An assistant exceeding its mandate is a scope problem for a person to fix.",
)
governance.policy(
    "Record improper output handling",
    when=[presence(OUTPUT_HANDLING)],
    lane="record",
)
# Core vocabulary, present in every workspace: no Canon needed.
governance.policy(
    "Review an escalating conversation",
    when=[strength("rt_pattern_escalation_v1", ">=", 0.5)],
    lane="coordinate",
    level="review",
)

governance.sdk_key("ollama-chatbot app", env_var="DMZAGENT_APP_KEY")
governance.env("BOT_SUBJECT_SLUG", "harbor-assistant")

governance.expect(
    "disclosure opens the breaker",
    labels={DISCLOSURE: 0.6}, enforce="block",
    policies=["Block on sensitive information disclosure"],
)
governance.expect(
    "mild injection flags, strong injection holds",
    labels={INJECTION: 0.8}, enforce="hold",
    policies=["Flag suspected prompt injection", "Hold the assistant on strong prompt injection"],
)
governance.expect(
    "excessive agency asks a person",
    labels={EXCESSIVE_AGENCY: 0.7}, coordinate="escalate",
)
