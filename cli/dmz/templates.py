"""The Solution Manifests ``dmz init`` writes. Each is a working starting
point for one shape of application; the comments say what to change first.

Placeholders: ``${DMZAGENT_VENDOR}``, ``${DMZAGENT_DIVISION_ID}`` and
``${DMZAGENT_WORKSPACE_ID}`` are filled from the key's own context at submit
time, so the same file works for whoever applies it; ``${posture:-enforce}``
takes ``--var posture=observe``.
"""
from __future__ import annotations

from string import Template

TEMPLATES: dict[str, tuple[str, str]] = {}


def _register(name: str, summary: str, body: str) -> None:
    TEMPLATES[name] = (summary, body)


def render(name: str, *, project: str) -> str:
    _, body = TEMPLATES[name]
    return Template(body).substitute(project=project, slug=project.lower().replace(" ", "-"))


_HEAD = """\
# Solution Manifest for $project ($name).
# The platform validates, plans and applies this file; it owns the Stack,
# computes the Change Set, enforces maker-checker and the vendor guardrails,
# and anchors every version on the ledger.
#
#   dmz validate                     # schema, references, guardrail preview
#   dmz plan                         # the Change Set, read-only
#   dmz apply --approved-by <reviewer> --write-env app/.env
#   dmz verify                       # the expectations below, on the platform's evaluator
#
apiVersion: dmzagent.com/v1
kind: SolutionManifest
metadata:
  name: $slug
  vendor: $${DMZAGENT_VENDOR}         # filled from your key
  version: 1
spec:
  divisions:
    - id: main
      division_id: $${DMZAGENT_DIVISION_ID}   # adopt the division your key belongs to
      config:
        reasoning_mode: per_frame              # reason on every message
        enforcement_posture: $${posture:-enforce}   # observe | warn | enforce
"""

_ROLES = """\
  # Segregation of duties: the vendor guardrail requires an independent auditor.
  roles:
    - {principal: auditor@$slug.example, role: auditor, scope: main}
"""

_register("chatbot", "the platform's hosted chat agent on your site", _HEAD.replace("$name", "hosted chat agent") + """\
  corpora:
    - id: support-corpus
      reasoning_canons: ["library/cn_seed_openai_agent_safety@latest"]   # prompt injection, PII leak, tool misuse, scope creep
  workspaces:
    - id: support
      workspace_id: $${DMZAGENT_WORKSPACE_ID}   # adopt the workspace your key is bound to
      division: main
      engine: reasoning
      corpus: support-corpus
  # Breaker rules on the agent's own subject: what stops the agent outright.
  circuit_breaker_policies:
    - id: block-pii-leak
      workspace: support
      name: Block on PII leak
      rules: [{tag: rt_agent_pii_leak_v1, op: ">=", value: 0.6}]
      action: block
    - id: review-scope-creep
      workspace: support
      name: Review on scope creep
      rules: [{tag: rt_agent_scope_creep_v1, op: ">=", value: 0.5}]
      action: review
  # Response lanes: a recoverable hold plus a review for a person.
  policies:
    - id: hold-scope-creep
      workspace: support
      name: Hold on scope creep
      when: [{kind: strength, label: rt_agent_scope_creep_v1, op: ">=", threshold: 0.7}]
      lane: enforce
      level: hold
    - id: ask-about-scope-creep
      workspace: support
      name: Ask a person about scope creep
      when: [{kind: presence, label: rt_agent_scope_creep_v1}]
      lane: coordinate
      level: review
  # The hosted agent itself: the platform serves the widget, the site embeds it.
  chatbots:
    - id: support-bot
      workspace: support
      agent_name: $project support
      site_domain: $${site_domain:-localhost}
      protected_action: refund.issue          # what a held conversation cannot do
      system_prompt: You are the $project support assistant. Be brief and kind.
""" + _ROLES + """\
  # Policy tests, run by the platform's own evaluator on `dmz verify`.
  expectations:
    - id: scope-creep-is-held-and-reviewed
      workspace: support
      labels: {rt_agent_scope_creep_v1: 0.8}
      enforce: hold
      coordinate: review
""")

_register("agent", "an agent on the platform's runtime, defined in code", _HEAD.replace("$name", "platform agent") + """\
  corpora:
    - id: agent-corpus
      reasoning_canons: ["library/cn_seed_openai_agent_safety@latest"]
  workspaces:
    - id: agents
      workspace_id: $${DMZAGENT_WORKSPACE_ID}
      division: main
      engine: reasoning
      corpus: agent-corpus
  # Tool misuse stops the agent; scope creep asks a person before it continues.
  circuit_breaker_policies:
    - id: block-tool-misuse
      workspace: agents
      name: Block on tool misuse
      rules: [{tag: rt_agent_tool_misuse_v1, op: ">=", value: 0.6}]
      action: block
    - id: review-scope-creep
      workspace: agents
      name: Review on scope creep
      rules: [{tag: rt_agent_scope_creep_v1, op: ">=", value: 0.5}]
      action: review
  policies:
    - id: ask-about-escalation
      workspace: agents
      name: Ask a person about an escalation
      when: [{kind: presence, label: rt_pattern_escalation_v1}]
      lane: coordinate
      level: review
""" + _ROLES + """\
  expectations:
    - id: tool-misuse-is-blocked
      workspace: agents
      labels: {rt_agent_tool_misuse_v1: 0.9}
      policies: [Block on tool misuse]
""")

_register("sdk-app", "your own application with the SDK embedded", _HEAD.replace("$name", "SDK-governed application") + """\
  corpora:
    - id: app-corpus
      reasoning_canons: ["library/cn_owasp_llm_top10@latest"]   # the OWASP LLM Top 10 vocabulary, free
  workspaces:
    - id: app
      workspace_id: $${DMZAGENT_WORKSPACE_ID}
      division: main
      engine: reasoning
      corpus: app-corpus
  circuit_breaker_policies:
    - id: block-disclosure
      workspace: app
      name: Block on disclosure
      rules: [{tag: rt_owasp_llm02_sensitive_information_disclosure_2025_v1, op: ">=", value: 0.5}]
      action: block
    - id: review-injection
      workspace: app
      name: Review on injection
      rules: [{tag: rt_owasp_llm01_prompt_injection_2025_v1, op: ">=", value: 0.5}]
      action: review
  policies:
    - id: hold-strong-injection
      workspace: app
      name: Hold on strong injection
      when: [{kind: strength, label: rt_owasp_llm01_prompt_injection_2025_v1, op: ">=", threshold: 0.75}]
      lane: enforce
      level: hold
    - id: escalate-excessive-agency
      workspace: app
      name: Escalate excessive agency
      when: [{kind: strength, label: rt_owasp_llm06_excessive_agency_2025_v1, op: ">=", threshold: 0.5}]
      lane: coordinate
      level: escalate
""" + _ROLES + """\
  expectations:
    - id: strong-injection-is-held
      workspace: app
      labels: {rt_owasp_llm01_prompt_injection_2025_v1: 0.9}
      enforce: hold
    - id: disclosure-trips-the-breaker
      workspace: app
      labels: {rt_owasp_llm02_sensitive_information_disclosure_2025_v1: 0.6}
      policies: [Block on disclosure]
""")

_register("desk", "an operations desk: hold, review, release", _HEAD.replace("$name", "operations desk") + """\
  corpora:
    - id: desk-corpus
      reasoning_canons: ["library/cn_seed_openai_agent_safety@latest"]
  workspaces:
    - id: desk
      workspace_id: $${DMZAGENT_WORKSPACE_ID}
      division: main
      engine: reasoning
      corpus: desk-corpus
  # A hard stop for a leak; a recoverable hold plus a review for tool misuse;
  # an escalation for deception; a webhook when remediation is due.
  circuit_breaker_policies:
    - id: block-pii-leak
      workspace: desk
      name: Block on PII leak
      rules: [{tag: rt_agent_pii_leak_v1, op: ">=", value: 0.6}]
      action: block
  policies:
    - id: hold-tool-misuse
      workspace: desk
      name: Hold on tool misuse
      when: [{kind: strength, label: rt_agent_tool_misuse_v1, op: ">=", threshold: 0.5}]
      lane: enforce
      level: hold
    - id: review-tool-misuse
      workspace: desk
      name: Review tool misuse
      when: [{kind: presence, label: rt_agent_tool_misuse_v1}]
      lane: coordinate
      level: review
    - id: escalate-deception
      workspace: desk
      name: Escalate deception
      when: [{kind: strength, label: rt_agent_deception_v1, op: ">=", threshold: 0.6}]
      lane: coordinate
      level: escalate
    - id: notify-remediation
      workspace: desk
      name: Notify remediation
      when: [{kind: strength, label: rt_agent_pii_leak_v1, op: ">=", threshold: 0.6}]
      lane: remediate
      level: webhook
      config:
        delivery: {endpoint: "$${remediation_url:-http://localhost:8002/hooks/remediate}", method: POST}
""" + _ROLES + """\
  expectations:
    - id: tool-misuse-holds-and-asks
      workspace: desk
      labels: {rt_agent_tool_misuse_v1: 0.7}
      enforce: hold
      coordinate: review
    - id: a-leak-is-a-hard-stop
      workspace: desk
      labels: {rt_agent_pii_leak_v1: 0.8}
      policies: [Block on PII leak]
""")

_register("logic", "a rulebook on the deterministic logic engine (logic workspace)", """\
# Solution Manifest for $project (logic rulebook).
# Needs a LOGIC workspace. The rulebook below is a private Logic Canon: the
# platform publishes a new version whenever its content changes and installs
# that version into the workspace. Rules are predicates over event fields; an
# `accumulate` block turns repeated matches into a decaying strength with bands
# that escalate record -> enforce -> human.
apiVersion: dmzagent.com/v1
kind: SolutionManifest
metadata:
  name: $slug
  vendor: $${DMZAGENT_VENDOR}
  version: 1
spec:
  divisions:
    - id: main
      division_id: $${DMZAGENT_DIVISION_ID}
      config: {enforcement_posture: $${posture:-enforce}}
  logic_canons:
    - id: rulebook
      slug: $slug
      name: $project rulebook
      rulebook:
        rules:
          - id: over-limit
            match: {predicate: comparison, field: value, gt: 85}
            accumulate: {weight: 1, half_life: 10m, bands: {record: 1, enforce: 3, human: 5}}
            on_true: [{disposition: enforce, level: hold, tag: over_limit}]
          - id: offline
            match: {predicate: value, field: status, in: [offline, fault]}
            on_true: [{disposition: coordinate, level: review, tag: offline}]
  corpora:
    - id: fleet-corpus
      logic_canons: [rulebook]                # the inline canon above, pinned at apply
  workspaces:
    - id: fleet
      workspace_id: $${DMZAGENT_WORKSPACE_ID}
      division: main
      engine: logic
      corpus: fleet-corpus
  # Policies on the logic soul: the hard stop the rulebook does not name.
  policies:
    - id: block-sustained-over-limit
      workspace: fleet
      name: Block on sustained over-limit
      when: [{kind: strength, label: over_limit, op: ">=", threshold: 6}]
      lane: enforce
      level: block
      soul: logic
    - id: review-offline
      workspace: fleet
      name: Review an offline sensor
      when: [{kind: presence, label: offline}]
      lane: coordinate
      level: review
      soul: logic
""" + _ROLES + """\
  expectations:
    - id: sustained-over-limit-blocks
      workspace: fleet
      labels: {over_limit: 6}
      soul: logic
      enforce: block
""")

_register("mcp", "an agent on the platform's MCP server", _HEAD.replace("$name", "MCP agent") + """\
  corpora:
    - id: agent-corpus
      reasoning_canons: ["library/cn_seed_openai_agent_safety@latest"]
  workspaces:
    - id: agents
      workspace_id: $${DMZAGENT_WORKSPACE_ID}
      division: main
      engine: reasoning
      corpus: agent-corpus
  # The agent calls enforce_covenant before it acts; the verdict is the breaker
  # on the subject, and this is what moves that breaker.
  circuit_breaker_policies:
    - id: block-pii-leak
      workspace: agents
      name: Block on PII leak
      rules: [{tag: rt_agent_pii_leak_v1, op: ">=", value: 0.6}]
      action: block
    - id: review-scope-creep
      workspace: agents
      name: Review on scope creep
      rules: [{tag: rt_agent_scope_creep_v1, op: ">=", value: 0.5}]
      action: review
  policies:
    - id: hold-tool-misuse
      workspace: agents
      name: Hold on tool misuse
      when: [{kind: strength, label: rt_agent_tool_misuse_v1, op: ">=", threshold: 0.5}]
      lane: enforce
      level: hold
    - id: ask-about-tool-misuse
      workspace: agents
      name: Ask a person about tool misuse
      when: [{kind: presence, label: rt_agent_tool_misuse_v1}]
      lane: coordinate
      level: review
""" + _ROLES + """\
  expectations:
    - id: tool-misuse-holds-and-asks
      workspace: agents
      labels: {rt_agent_tool_misuse_v1: 0.7}
      enforce: hold
      coordinate: review
""")
