"""The declarative model: what a governance file can declare.

A governance file is ordinary Python that builds one `Governance` object.
Each method on it declares a resource the platform should hold — a breaker
policy, a response policy, a chatbot definition, a logic rulebook, a division
setting, an application key — and returns a handle whose outputs (ids the
platform assigns) can be wired into the application's environment.

Everything here is data. Nothing talks to the platform; `engine` does.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

VALID_POSTURES = ("observe", "warn", "enforce")
VALID_REASONING_MODES = ("per_frame", "per_trace")
BREAKER_ACTIONS = ("allow", "review", "block")
BREAKER_SCOPES = ("subject", "interaction")
OPERATORS = (">=", ">", "<=", "<", "==", "!=")
SOULS = ("reasoning", "logic", "any")
CONDITION_KINDS = ("presence", "strength")
LANES: dict[str, tuple[str, ...]] = {
    "record": (),
    "enforce": ("allow", "challenge", "hold", "block"),
    "remediate": ("revoke", "disable", "ticket", "webhook"),
    "coordinate": ("notify", "review", "escalate"),
}


class DeclarationError(ValueError):
    """The governance file declares something the platform would refuse.
    Raised while the file is being built, so the mistake is reported where
    it was written rather than as a 400 halfway through an apply."""


@dataclass(frozen=True)
class Ref:
    """A reference to an output a resource will have once applied."""
    resource: "Resource"
    output: str

    def resolve(self) -> Any:
        if self.output not in self.resource.outputs:
            raise KeyError(f"{self.resource.ident} has no output {self.output!r} yet")
        return self.resource.outputs[self.output]


@dataclass
class Resource:
    kind: str
    name: str
    outputs: dict = field(default_factory=dict)

    @property
    def ident(self) -> str:
        return f"{self.kind}/{self.name}"

    def ref(self, output: str) -> Ref:
        return Ref(self, output)

    def desired(self) -> dict:
        """The platform-facing shape, for diffing. Subclasses override."""
        return {}


@dataclass
class DivisionConfig(Resource):
    settings: dict = field(default_factory=dict)

    def desired(self) -> dict:
        return dict(self.settings)


@dataclass
class CanonRequirement(Resource):
    canon_id: str = ""
    version: str | None = None
    why: str = ""


@dataclass
class BreakerPolicy(Resource):
    rules: list[dict] = field(default_factory=list)
    action: str = "block"
    scope: str = "subject"
    description: str | None = None
    enabled: bool = True

    def desired(self) -> dict:
        return {"name": self.name, "rules": [dict(r) for r in self.rules],
                "action": self.action, "scope": self.scope,
                "description": self.description, "enabled": self.enabled}


@dataclass
class Policy(Resource):
    conditions: list[dict] = field(default_factory=list)
    lane: str = "record"
    level: str | None = None
    soul: str = "reasoning"
    config: dict = field(default_factory=dict)
    description: str | None = None
    enabled: bool = True

    def desired(self) -> dict:
        return {"name": self.name, "conditions": [dict(c) for c in self.conditions],
                "lane": self.lane, "level": self.level, "soul": self.soul,
                "config": dict(self.config), "description": self.description,
                "enabled": self.enabled}


@dataclass
class Chatbot(Resource):
    site_domain: str = ""
    protected_action: str | None = None
    config: dict = field(default_factory=dict)

    def desired(self) -> dict:
        return {"agent_name": self.name, "site_domain": self.site_domain,
                "protected_action": self.protected_action, "config": dict(self.config)}


@dataclass
class LogicRulebook(Resource):
    slug: str = ""
    rulebook: dict = field(default_factory=dict)
    description: str = ""

    def desired(self) -> dict:
        return {"slug": self.slug, "rulebook": canonical_rulebook(self.rulebook)}


@dataclass
class SdkKey(Resource):
    env_var: str = "DMZAGENT_APP_KEY"
    label: str = ""


@dataclass
class Expectation:
    """A policy test: given a soul, the engine must resolve to this."""
    name: str
    labels: dict[str, float]
    fired: list[str] = field(default_factory=list)
    soul: str = "reasoning"
    enforce: str | None = None
    coordinate: str | None = None
    remediate: str | None = None
    policies: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------- #
# Condition helpers for response policies
# ------------------------------------------------------------------------- #


def presence(label: str) -> dict:
    """The label is on the subject's soul at all."""
    if not label:
        raise DeclarationError("presence() needs a label")
    return {"kind": "presence", "label": label}


def strength(label: str, op: str, threshold: float) -> dict:
    """The label's strength (0.0 to 1.0 on the reasoning soul) crosses a line."""
    if not label:
        raise DeclarationError("strength() needs a label")
    if op not in OPERATORS:
        raise DeclarationError(f"strength() op must be one of {OPERATORS}; got {op!r}")
    return {"kind": "strength", "label": label, "op": op, "threshold": float(threshold)}


def canonical_rulebook(rulebook: dict) -> dict:
    """A rulebook without the identity fields the platform stamps on publish,
    so a declared rulebook and a published one compare on content alone."""
    doc = {k: v for k, v in (rulebook or {}).items()
           if k not in ("logic_canon", "canon", "version", "visibility")}
    return json.loads(json.dumps(doc, sort_keys=True))


# ------------------------------------------------------------------------- #
# The declaration
# ------------------------------------------------------------------------- #


class Governance:
    """One governance declaration for one workspace."""

    def __init__(self, name: str, description: str = ""):
        if not name or "/" in name or " " in name:
            raise DeclarationError("a governance needs a short name without spaces")
        self.name = name
        self.description = description
        self.resources: list[Resource] = []
        self.expectations: list[Expectation] = []
        self.env_outputs: dict[str, Ref | str] = {}
        self._division_config: DivisionConfig | None = None

    # --- resources ----------------------------------------------------- #

    def _add(self, resource: Resource) -> Resource:
        for existing in self.resources:
            if existing.ident == resource.ident:
                raise DeclarationError(f"{resource.ident} is declared twice")
        self.resources.append(resource)
        return resource

    def division_config(self, **settings: Any) -> DivisionConfig:
        """Operator settings on the division. Typed keys are validated here:
        reasoning_mode (per_frame | per_trace), enforcement_posture
        (observe | warn | enforce). Other keys pass through untouched."""
        posture = settings.get("enforcement_posture")
        if posture is not None and posture not in VALID_POSTURES:
            raise DeclarationError(
                f"enforcement_posture must be one of {VALID_POSTURES}; got {posture!r}")
        mode = settings.get("reasoning_mode")
        if mode is not None and mode not in VALID_REASONING_MODES:
            raise DeclarationError(
                f"reasoning_mode must be one of {VALID_REASONING_MODES}; got {mode!r}")
        if self._division_config is not None:
            self._division_config.settings.update(settings)
            return self._division_config
        self._division_config = DivisionConfig(kind="division_config", name="division",
                                               settings=dict(settings))
        self._add(self._division_config)
        return self._division_config

    def require_canon(self, canon_id: str, *, version: str | None = None,
                      why: str = "") -> CanonRequirement:
        """A Canon (tag vocabulary + starter policies) the workspace must have
        installed. API keys cannot install Canons; the plan reports whether
        it is there and, if not, what to do in the console."""
        if not canon_id:
            raise DeclarationError("require_canon() needs a canon_id")
        return self._add(CanonRequirement(kind="canon", name=canon_id, canon_id=canon_id,
                                          version=version, why=why))

    def breaker_policy(self, name: str, *, rules: list, action: str,
                       scope: str = "subject", description: str | None = None,
                       enabled: bool = True) -> BreakerPolicy:
        """A circuit-breaker rule: when every clause matches the subject's
        soul, the breaker moves. `rules` is a list of (tag, op, value)."""
        if action not in BREAKER_ACTIONS:
            raise DeclarationError(f"action must be one of {BREAKER_ACTIONS}; got {action!r}")
        if scope not in BREAKER_SCOPES:
            raise DeclarationError(f"scope must be one of {BREAKER_SCOPES}; got {scope!r}")
        clean: list[dict] = []
        for i, rule in enumerate(rules):
            if isinstance(rule, dict):
                tag, op, value = rule.get("tag"), rule.get("op"), rule.get("value")
            else:
                try:
                    tag, op, value = rule
                except (TypeError, ValueError):
                    raise DeclarationError(f"rules[{i}] must be (tag, op, value)") from None
            if not tag or op not in OPERATORS:
                raise DeclarationError(f"rules[{i}]: tag required and op in {OPERATORS}")
            clean.append({"tag": str(tag), "op": op, "value": float(value)})
        return self._add(BreakerPolicy(kind="breaker_policy", name=name, rules=clean,
                                       action=action, scope=scope,
                                       description=description, enabled=enabled))

    def policy(self, name: str, *, when: list[dict], lane: str, level: str | None = None,
               soul: str = "reasoning", config: dict | None = None,
               description: str | None = None, enabled: bool = True) -> Policy:
        """A response policy from the closed catalog: conditions on a soul →
        one of record, enforce(allow|challenge|hold|block),
        remediate(revoke|disable|ticket|webhook), coordinate(notify|review|escalate)."""
        if lane not in LANES:
            raise DeclarationError(f"lane must be one of {tuple(LANES)}; got {lane!r}")
        if lane == "record":
            if level is not None:
                raise DeclarationError("the record lane takes no level")
        elif level not in LANES[lane]:
            raise DeclarationError(f"level for {lane!r} must be one of {LANES[lane]}; got {level!r}")
        if soul not in SOULS:
            raise DeclarationError(f"soul must be one of {SOULS}; got {soul!r}")
        if not when:
            raise DeclarationError("a policy needs at least one condition (presence()/strength())")
        for c in when:
            if not isinstance(c, dict) or c.get("kind") not in CONDITION_KINDS:
                raise DeclarationError("conditions come from presence() or strength()")
        return self._add(Policy(kind="policy", name=name, conditions=[dict(c) for c in when],
                                lane=lane, level=level, soul=soul, config=dict(config or {}),
                                description=description, enabled=enabled))

    def chatbot(self, agent_name: str, *, site_domain: str = "", system_prompt: str = "",
                protected_action: str | None = None, allow_any_origin: bool = False,
                config: dict | None = None) -> Chatbot:
        """A hosted chat agent the platform serves to a site. The widget only
        answers requests from `site_domain` (or any origin, development only)."""
        if not site_domain and not allow_any_origin:
            raise DeclarationError(
                "chatbot() needs a site_domain, or allow_any_origin=True for development")
        cfg = dict(config or {})
        if system_prompt:
            cfg["system_prompt"] = system_prompt
        if allow_any_origin:
            cfg["allow_any_origin"] = True
        return self._add(Chatbot(kind="chatbot", name=agent_name, site_domain=site_domain,
                                 protected_action=protected_action, config=cfg))

    def logic_rulebook(self, name: str, *, slug: str, rulebook: dict,
                       description: str = "") -> LogicRulebook:
        """A deterministic rulebook (Logic Canon), published as a new version
        whenever its content changes and installed into the workspace."""
        if not slug or not rulebook.get("rules"):
            raise DeclarationError("logic_rulebook() needs a slug and a rulebook with rules")
        return self._add(LogicRulebook(kind="logic_rulebook", name=name, slug=slug,
                                       rulebook=dict(rulebook), description=description))

    def sdk_key(self, label: str, *, env_var: str = "DMZAGENT_APP_KEY") -> SdkKey:
        """An analyst-role key for the application, minted once and written
        to the env file. The applier's tenant_admin key never leaves the
        operator's hands."""
        if not label or not env_var.isidentifier():
            raise DeclarationError("sdk_key() needs a label and an env_var name")
        return self._add(SdkKey(kind="sdk_key", name=label, label=label, env_var=env_var))

    # --- tests and outputs --------------------------------------------- #

    def expect(self, name: str, *, labels: dict[str, float], fired: list[str] | None = None,
               soul: str = "reasoning", enforce: str | None = None,
               coordinate: str | None = None, remediate: str | None = None,
               policies: list[str] | None = None) -> Expectation:
        """What the policy engine must resolve for a given soul. Checked by
        `giaas verify` with the platform's own dry-evaluate, so the policies
        are tested by the engine that will run them."""
        if soul not in ("reasoning", "logic"):
            raise DeclarationError("expect() soul must be reasoning or logic")
        exp = Expectation(name=name, labels=dict(labels), fired=list(fired or []), soul=soul,
                          enforce=enforce, coordinate=coordinate, remediate=remediate,
                          policies=list(policies or []))
        self.expectations.append(exp)
        return exp

    def env(self, var: str, value: Ref | str) -> None:
        """Write an output into the application's env file after apply."""
        if not var.isidentifier():
            raise DeclarationError(f"{var!r} is not a valid environment variable name")
        self.env_outputs[var] = value

    def by_kind(self, kind: str) -> list[Resource]:
        return [r for r in self.resources if r.kind == kind]
