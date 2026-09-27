"""A client for the platform's MCP server: JSON-RPC 2.0 over ``POST /mcp/v1``,
bearer-authenticated with an API key, paced under the vendor rate limit.

The server calls itself Concordia and answers protocol version "1.0". Its
tools return ``{content: [...], _data: {...}, isError}``; :func:`unwrap`
gives back the structured ``_data`` so callers never parse text they were
handed as JSON.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from giaas.client import Platform, PlatformError  # noqa: F401  (re-exported for callers)

# Tools whose handlers never write; the bridge marks them read-only for hosts.
READ_ONLY_TOOLS = frozenset({
    "query_corpus", "get_subject_soul", "get_division_config",
    "get_notification_prefs", "list_notification_patterns", "get_trace_patterns",
})

# JSON-RPC application errors the server documents (spec §8) and what they mean.
ERROR_MEANINGS = {
    "auth_expired": "the key was rejected: invalid, revoked, or missing the mcp scope",
    "tenant_quota_exceeded": "the workspace is over its quota or cost cap",
    "policy_engine_unavailable": "the breaker could not be consulted; treat as block",
    "canon_not_installed": "the Canon is not installed in this workspace (console: Library)",
    "subject_not_found": "no soul snapshot exists for that subject yet",
    "circuit_open": "the per-tool rate limit tripped; wait and retry",
    "permission_denied": "the key's role cannot call this (writes need analyst, config needs tenant_admin)",
}


class McpError(Exception):
    """A JSON-RPC error envelope from the server."""

    def __init__(self, code: int | None, message: str, data: dict | None = None, method: str | None = None):
        self.code = code
        self.message = message
        self.data = data or {}
        self.method = method
        self.error_id = self.data.get("error_id")
        super().__init__(message)

    def __str__(self) -> str:
        tail = f" [{self.error_id}]" if self.error_id else ""
        return f"{self.message}{tail}"

    @property
    def meaning(self) -> str | None:
        return ERROR_MEANINGS.get(self.error_id or "")


def unwrap(result: Any) -> Any:
    """The structured payload of a tools/call result."""
    if not isinstance(result, dict):
        return result
    if "_data" in result:
        return result["_data"]
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                return json.loads(block.get("text") or "")
            except ValueError:
                return block.get("text")
    return result


class McpClient:
    def __init__(self, base_url: str, api_key: str, *, transport: Callable | None = None,
                 min_interval: float = 0.125, log: Callable[[str], None] | None = None,
                 client_name: str = "dmz", client_version: str = "0.1.0"):
        self.platform = Platform(api_key, base_url, transport=transport, min_interval=min_interval, log=log)
        self.base_url = self.platform.base_url
        self.client_name = client_name
        self.client_version = client_version
        self.server_info: dict | None = None
        self.principal: dict | None = None
        self.protocol_version: str | None = None
        self._id = 0
        self._tools: list[dict] | None = None
        self._resources: list[dict] | None = None

    # -- transport -------------------------------------------------------
    def rpc(self, method: str, params: dict | None = None) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        _, data = self.platform.request("POST", "/mcp/v1", body)
        if not isinstance(data, dict):
            raise McpError(-32603, "the server did not answer with a JSON-RPC envelope", method=method)
        err = data.get("error")
        if err:
            raise McpError(err.get("code"), err.get("message") or "error", err.get("data"), method)
        return data.get("result")

    # -- lifecycle -------------------------------------------------------
    def initialize(self) -> dict:
        result = self.rpc("initialize", {
            "protocolVersion": "1.0",
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": self.client_version},
        }) or {}
        self.server_info = result.get("serverInfo") or {}
        self.principal = result.get("principalHint") or {}
        self.protocol_version = result.get("protocolVersion")
        return result

    def ping(self) -> bool:
        return bool((self.rpc("ping") or {}).get("ok"))

    # -- tools -----------------------------------------------------------
    def tools(self, refresh: bool = False) -> list[dict]:
        if self._tools is None or refresh:
            self._tools = list((self.rpc("tools/list") or {}).get("tools") or [])
        return self._tools

    def call(self, name: str, arguments: dict | None = None) -> Any:
        """Call a tool and return its structured result (``_data``)."""
        return unwrap(self.rpc("tools/call", {"name": name, "arguments": arguments or {}}))

    def call_raw(self, name: str, arguments: dict | None = None) -> dict:
        """Call a tool and return the server's full result envelope."""
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}}) or {}

    # -- resources -------------------------------------------------------
    def resources(self, refresh: bool = False) -> list[dict]:
        if self._resources is None or refresh:
            self._resources = list((self.rpc("resources/list") or {}).get("resources") or [])
        return self._resources

    def read_raw(self, uri: str) -> dict:
        return self.rpc("resources/read", {"uri": uri}) or {}

    def read(self, uri: str) -> Any:
        """Read a resource and return its JSON document (or text)."""
        for block in self.read_raw(uri).get("contents") or []:
            text = block.get("text")
            if text is None:
                continue
            try:
                return json.loads(text)
            except ValueError:
                return text
        return None

    # -- the two calls every governed agent makes ------------------------
    def enforce(self, subject_id: str, action_kind: str, action_payload: dict | None = None,
                context: dict | None = None) -> dict:
        return self.call("enforce_covenant", {
            "subject_id": subject_id, "action_kind": action_kind,
            "action_payload": action_payload or {}, "context": context or {}})

    def record(self, subject_id: str, decision_kind: str, payload: dict | None = None, *,
               actor: str = "agent", outcome: str = "completed") -> dict:
        return self.call("record_decision", {
            "subject_id": subject_id, "decision_kind": decision_kind,
            "payload": payload or {}, "actor": actor, "outcome": outcome})
