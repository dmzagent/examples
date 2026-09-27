"""A small client for the platform's agent runtime, and a way to define an
agent in code.

The platform's runtime rule is that the customer supplies intent — a signed
"dogma" manifest: charter, privilege, allow-lists and a node graph called the
spine — and none of the customer's code runs in the platform's cloud. A step
marked `where="client"` is the other end of that boundary: the platform
offers it to a runner on your machine as a lease, the runner executes it
here, and only what it returns crosses back.

This module is standard library only: `AgentClient` wraps the `/v1/agents`,
`/v1/agent-jobs` and `/v1/agent-sessions` endpoints, `Agent` turns decorated
functions into a dogma, and `Runner` serves the client steps.
"""
from __future__ import annotations

import inspect
import json
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

CLOUD = "cloud"
CLIENT = "client"


class AgentApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- #
# The HTTP client
# --------------------------------------------------------------------------- #


class AgentClient:
    """One workspace key against one platform deployment."""

    def __init__(self, api_key: str, base_url: str, *, timeout: float = 900.0):
        if not api_key.startswith("ck_"):
            raise AgentApiError("the API key must be a DMZAgent key (ck_…)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._last = 0.0

    def call(self, method: str, path: str, body: Any = None, **query: Any) -> Any:
        url = self.base_url + path
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"authorization": f"Bearer {self.api_key}", "accept": "application/json",
                   "user-agent": "dmzagent-examples/platform-agent"}
        if data is not None:
            headers["content-type"] = "application/json"
        for attempt in range(6):
            # Stay under the per-vendor burst limit; wait out a 429 if we hit it.
            wait = 0.12 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    parsed = json.loads(raw) if raw else {}
                except ValueError:
                    parsed = {"detail": raw.decode("utf-8", "replace")[:300]}
                if exc.code == 429 and attempt < 5:
                    time.sleep(float(exc.headers.get("Retry-After") or parsed.get("retry_after") or 1) + 0.1)
                    continue
                detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
                raise AgentApiError(f"{method} {path} -> {exc.code}: {detail}", status=exc.code, detail=detail)
            except urllib.error.URLError as exc:
                raise AgentApiError(f"could not reach {url}: {exc.reason}") from exc
        raise AgentApiError(f"{method} {path}: rate limit did not clear", status=429)

    # --- definition and lifecycle ------------------------------------------ #

    def create_agent(self, name: str, *, division_id: str | None, workspace_id: str | None,
                     instance_class: str = "std-std") -> dict:
        return self.call("POST", "/v1/agents", {"name": name, "division_id": division_id,
                                                "workspace_id": workspace_id,
                                                "instance_class": instance_class, "agent_kind": "custom"})

    def list_agents(self) -> list[dict]:
        return self.call("GET", "/v1/agents").get("agents") or []

    def get_agent(self, agent_id: str) -> dict:
        return self.call("GET", f"/v1/agents/{agent_id}")

    def set_dogma(self, agent_id: str, dogma: dict) -> dict:
        # The manifest itself is the body, not {"dogma": …}.
        return self.call("PUT", f"/v1/agents/{agent_id}/dogma", dogma)

    def add_scenario(self, agent_id: str, *, name: str, match: list[dict], output: dict) -> dict:
        return self.call("POST", f"/v1/agents/{agent_id}/scenarios",
                         {"name": name, "match": match, "output": output})

    def list_scenarios(self, agent_id: str) -> list[dict]:
        return self.call("GET", f"/v1/agents/{agent_id}/scenarios").get("scenarios") or []

    def grade_from_training(self, agent_id: str) -> dict:
        return self.call("POST", f"/v1/agents/{agent_id}/grade-from-training", {})

    def arm(self, agent_id: str) -> dict:
        return self.call("POST", f"/v1/agents/{agent_id}/arm", {})

    def disarm(self, agent_id: str) -> dict:
        return self.call("POST", f"/v1/agents/{agent_id}/disarm", {})

    def dispatch(self, agent_id: str, inputs: dict) -> dict:
        """Deterministic auto-mode: a trained input returns its taught output
        with no model call; anything off-path escalates."""
        return self.call("POST", f"/v1/agents/{agent_id}/dispatch", {"input": inputs})

    def run(self, agent_id: str, trigger: dict | None = None) -> dict:
        return self.call("POST", f"/v1/agents/{agent_id}/run", {"trigger": trigger or {}})

    def job(self, job_id: str) -> dict:
        return self.call("GET", f"/v1/agent-jobs/{job_id}")

    # --- outputs and sign-off ---------------------------------------------- #

    def gate_output(self, agent_id: str, *, modality: str, name: str | None = None,
                    payload: dict | None = None, action_ref: str | None = None) -> dict:
        return self.call("POST", f"/v1/agents/{agent_id}/outputs",
                         {"modality": modality, "name": name, "payload": payload or {},
                          "action_ref": action_ref})

    def pending_outputs(self, agent_id: str) -> list[dict]:
        return self.call("GET", f"/v1/agents/{agent_id}/outputs/pending").get("pending") or []

    def list_outputs(self, agent_id: str) -> list[dict]:
        return self.call("GET", f"/v1/agents/{agent_id}/outputs").get("outputs") or []

    def confirm_output(self, output_id: str) -> dict:
        return self.call("POST", f"/v1/agent-outputs/{output_id}/confirm", {})

    def reject_output(self, output_id: str, reason: str) -> dict:
        return self.call("POST", f"/v1/agent-outputs/{output_id}/reject", {"reason": reason})

    # --- the client end: sessions and leases ------------------------------- #

    def open_session(self, *, label: str, agent_id: str | None = None) -> dict:
        return self.call("POST", "/v1/agent-sessions", {"label": label, "agent_id": agent_id,
                                                        "runner": "dmzagent-examples"})

    def next_step(self, session_id: str) -> dict | None:
        return self.call("POST", f"/v1/agent-sessions/{session_id}/next-step", {}).get("step")

    def post_result(self, session_id: str, lease_id: str, *, output: Any = None,
                    error: str | None = None) -> dict:
        return self.call("POST", f"/v1/agent-sessions/{session_id}/steps/{lease_id}/result",
                         {"output": output, "error": error})

    def close_session(self, session_id: str) -> dict:
        return self.call("DELETE", f"/v1/agent-sessions/{session_id}")


# --------------------------------------------------------------------------- #
# Defining an agent in code
# --------------------------------------------------------------------------- #


def arm_blocker(agent: dict) -> str | None:
    """Why this agent cannot run unattended, in plain words; None if it can."""
    if agent.get("status") == "armed":
        return None
    if not agent.get("dogma_version"):
        return "it has no dogma yet — deploy it first"
    if not agent.get("grade"):
        return "it has never been graded — add training scenarios and grade it"
    if agent.get("graded_version") != agent.get("dogma_version"):
        return (f"its dogma changed since it was graded (graded v{agent.get('graded_version')}, "
                f"now v{agent.get('dogma_version')}) — grade it again")
    return f"its grade is {agent.get('grade')}, below the B the arm gate requires"


@dataclass
class Step:
    id: str
    instruction: str
    where: str = CLOUD
    needs: tuple[str, ...] = ()
    max_turns: int | None = None
    max_seconds: float | None = None
    client_timeout_seconds: float | None = None
    fn: Callable | None = None  # only for client steps; never serialized

    def node(self) -> dict:
        node: dict = {"id": self.id, "needs": list(self.needs), "instruction": self.instruction}
        if self.where == CLIENT:
            node["where"] = CLIENT
        for key in ("max_turns", "max_seconds", "client_timeout_seconds"):
            if getattr(self, key) is not None:
                node[key] = getattr(self, key)
        return node


class Agent:
    """An agent whose cloud steps are intent (their docstrings) and whose
    client steps are code (their bodies, run here)."""

    def __init__(self, name: str, *, charter: str, privilege: str = "read_only",
                 output_schemas: list[dict] | None = None, writes: str = "human_confirm"):
        self.name = name
        self.charter = charter
        self.privilege = privilege
        self.output_schemas = list(output_schemas or [])
        self.writes = writes
        self.steps: list[Step] = []

    def step(self, *, where: str = CLOUD, needs: list | None = None, **limits: Any):
        def decorate(fn: Callable) -> Callable:
            doc = inspect.getdoc(fn)
            if not doc:
                raise ValueError(f"step {fn.__name__} needs a docstring: it is the instruction the agent gets")
            step_id = fn.__name__.replace("_", "-")
            resolved = tuple(getattr(n, "_step_id", n) for n in (needs or []))
            known = {s.id for s in self.steps}
            for n in resolved:
                if n not in known:
                    raise ValueError(f"step {step_id} needs {n!r}, which is not defined yet")
            self.steps.append(Step(id=step_id, instruction=textwrap.dedent(doc).strip(), where=where,
                                   needs=resolved, fn=fn if where == CLIENT else None, **limits))
            fn._step_id = step_id  # type: ignore[attr-defined]
            return fn
        return decorate

    def compile(self) -> dict:
        """The dogma manifest. Pure and offline: print it, diff it in review."""
        if not self.steps:
            raise ValueError("an agent with no steps has nothing to run")
        return {
            "charter": self.charter,
            "privilege": self.privilege,
            "bound_canons": [],
            "installed_skills": [],
            "connector_bindings": [],
            "disposition_policy": {"writes": self.writes},
            "output_schemas": list(self.output_schemas),
            "spine": {"nodes": [s.node() for s in self.steps]},
        }

    def client_steps(self) -> dict[str, Step]:
        return {s.id: s for s in self.steps if s.where == CLIENT and s.fn}


# --------------------------------------------------------------------------- #
# The runner — this machine's end of the boundary
# --------------------------------------------------------------------------- #


@dataclass
class Runner:
    """Pulls the agent's client steps from the platform and runs them here.

    Pull-only: the platform never connects to this machine. Use it as a
    context manager around `client.run(...)`: the spine blocks at a client
    step until a runner takes it, so the runner must be listening first.
    """
    agent: Agent
    client: AgentClient
    poll_seconds: float = 1.0
    session_id: str | None = None
    served: list[dict] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def __enter__(self) -> "Runner":
        self.session_id = self.client.open_session(label=self.agent.name)["session_id"]
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_seconds * 3)
        if self.session_id:
            try:
                self.client.close_session(self.session_id)
            except AgentApiError:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                took = self.poll_once()
            except AgentApiError as exc:
                self.served.append({"error": str(exc)})
                took = False
            if not took:
                self._stop.wait(self.poll_seconds)

    def poll_once(self) -> bool:
        assert self.session_id
        step = self.client.next_step(self.session_id)
        if not step:
            return False
        node_id = (step.get("node") or {}).get("id")
        impl = self.agent.client_steps().get(node_id)
        if impl is None:
            self.client.post_result(self.session_id, step["lease_id"],
                                    error=f"this runner has no local step named {node_id!r}")
            self.served.append({"node": node_id, "status": "unknown"})
            return True
        try:
            output = impl.fn(step.get("inputs") or {})
        except Exception as exc:  # noqa: BLE001 — reported to the platform, not swallowed
            # The type and message cross; the traceback, which names local
            # paths, stays here.
            traceback.print_exc()
            self.client.post_result(self.session_id, step["lease_id"],
                                    error=f"{type(exc).__name__}: {exc}"[:500])
            self.served.append({"node": node_id, "status": "failed"})
            return True
        self.client.post_result(self.session_id, step["lease_id"], output={"output": output})
        self.served.append({"node": node_id, "status": "done", "output": output})
        return True
