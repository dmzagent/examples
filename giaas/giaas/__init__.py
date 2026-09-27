"""giaas — Governance Infrastructure as a Service, for DMZAgent workspaces.

Declare the governance an application needs in one Python file, then:

    python -m giaas plan   governance.py    # what would change (read-only)
    python -m giaas apply  governance.py    # make it so; write ids and keys to .env
    python -m giaas verify governance.py    # requirements met? policies resolve as expected?
    python -m giaas destroy governance.py   # take it back out

A governance file looks like this:

    from giaas import Governance, presence, strength

    governance = Governance("support-bot")
    governance.division_config(reasoning_mode="per_frame", enforcement_posture="enforce")
    governance.require_canon("cn_seed_openai_agent_safety")
    governance.breaker_policy("Block on prompt injection",
        rules=[("rt_agent_prompt_injection_v1", ">=", 0.7)], action="block")
    governance.policy("Hold on scope creep",
        when=[strength("rt_agent_scope_creep_v1", ">=", 0.5)], lane="enforce", level="hold")
    governance.sdk_key("support-bot app", env_var="DMZAGENT_APP_KEY")

Standard library only. The applier authenticates with a tenant_admin API key
(DMZAGENT_API_KEY); it never stores that key, and the only secret it writes
is an application key it minted, into the env file you name.
"""
from __future__ import annotations

#: Values passed on the command line with --var KEY=VALUE, readable by a
#: governance file as `from giaas import settings`.
settings: dict[str, str] = {}

from .client import ConfigError, Platform, PlatformError, fingerprint  # noqa: E402
from .resources import (  # noqa: E402
    BreakerPolicy, CanonRequirement, Chatbot, DeclarationError, DivisionConfig, Expectation,
    Governance, LogicRulebook, Policy, Ref, SdkKey, presence, strength,
)
from .engine import (  # noqa: E402
    Change, Ctx, RequirementUnmet, apply, connect, destroy, plan, read_env_var, set_env_var, verify,
)

__version__ = "0.1.0"
__all__ = [
    "settings", "Governance", "presence", "strength", "Platform", "PlatformError", "ConfigError",
    "DeclarationError", "RequirementUnmet", "Change", "Ctx", "Ref", "connect", "plan", "apply",
    "verify", "destroy", "read_env_var", "set_env_var", "fingerprint", "BreakerPolicy",
    "CanonRequirement", "Chatbot", "DivisionConfig", "Expectation", "LogicRulebook", "Policy",
    "SdkKey", "__version__",
]
