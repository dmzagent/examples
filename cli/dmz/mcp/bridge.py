"""``dmz mcp bridge``: a standard MCP server on stdio, in front of the platform.

Claude Code, Claude Desktop, Cursor and every other MCP host speak the
Model Context Protocol: JSON-RPC 2.0 over stdio, a versioned ``initialize``
handshake, an ``initialized`` notification, and tool results shaped as
content blocks. The platform's server speaks JSON-RPC over HTTPS with its
own protocol version ("1.0") and does not accept the notification, so a
host cannot connect to it directly today. This bridge is the adapter: it
answers the standard handshake, forwards ``tools/*`` and ``resources/*``
to ``/mcp/v1`` with the key it was given, and reports tool failures the way
hosts expect (``isError`` results, not protocol errors).

Nothing here interprets the key; it is read from the environment, an env
file or a saved profile at start, sent as a bearer, and never logged.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, BinaryIO

from .client import McpClient, McpError, PlatformError, READ_ONLY_TOOLS, unwrap

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_VERSION = SUPPORTED_VERSIONS[0]

INSTRUCTIONS = (
    "DMZAgent governance for this workspace. Before an action that affects a "
    "subject (a customer, an account, a device), call enforce_covenant with the "
    "subject and the action; act only on verdict 'allow', ask a person on "
    "'review', and stop on 'block'. After an action, call record_decision so the "
    "ledger holds what was done. query_corpus finds the policies and risk tags "
    "that apply; get_subject_soul explains a verdict through the subject's "
    "current risk tags. Read concordia:/workspace/policies once to learn which "
    "breaker rules exist."
)

RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "concordia:/workspace/recent-ledger{?since,limit}",
        "name": "Recent ledger entries (paginated)",
        "description": "The workspace's hash-chained ledger from index `since`, at most `limit` entries (max 500).",
        "mimeType": "application/json",
    },
]

log = logging.getLogger("dmz.mcp.bridge")


class Bridge:
    """Handles one JSON-RPC message at a time; :meth:`serve` runs the stdio loop."""

    def __init__(self, client: McpClient, *, name: str = "dmzagent", version: str = "0.1.0"):
        self.client = client
        self.name = name
        self.version = version
        self.protocol_version = LATEST_VERSION
        self.initialized = False
        self.client_info: dict = {}
        self._upstream_note = ""

    # -- framing ---------------------------------------------------------
    def serve(self, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._write(stdout, _error(None, -32700, "Parse error"))
                continue
            if isinstance(message, list):
                replies = [r for r in (self.handle(m) for m in message) if r is not None]
                if replies:
                    self._write(stdout, replies)
                continue
            reply = self.handle(message)
            if reply is not None:
                self._write(stdout, reply)
        return 0

    @staticmethod
    def _write(stdout: BinaryIO, payload: Any) -> None:
        stdout.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        stdout.flush()

    # -- dispatch --------------------------------------------------------
    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")
        method = message.get("method")
        has_id = "id" in message
        jid = message.get("id")
        if not isinstance(method, str):
            # A response to a request we never sent, or garbage.
            return None if not has_id else _error(jid, -32600, "Invalid Request")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(jid, -32602, "params must be an object") if has_id else None
        if not has_id:
            self._notification(method, params)
            return None
        try:
            return _result(jid, self._request(method, params))
        except _Rpc as exc:
            return _error(jid, exc.code, exc.message, exc.data)
        except PlatformError as exc:
            log.error("platform request failed: %s", exc)
            return _error(jid, -32603, f"platform request failed: {exc}")
        except McpError as exc:
            return _error(jid, exc.code or -32603, str(exc), exc.data or None)

    def _notification(self, method: str, params: dict) -> None:
        if method == "notifications/initialized":
            self.initialized = True
            log.info("host initialized (%s)", self.client_info.get("name", "unknown host"))
        elif method == "notifications/cancelled":
            log.debug("host cancelled request %s", params.get("requestId"))
        else:
            log.debug("ignoring notification %s", method)

    def _request(self, method: str, params: dict) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [self._descriptor(t) for t in self.client.tools()]}
        if method == "tools/call":
            return self._call(params)
        if method == "resources/list":
            return {"resources": [
                {k: r.get(k) for k in ("uri", "name", "description", "mimeType") if r.get(k) is not None}
                for r in self.client.resources()]}
        if method == "resources/templates/list":
            return {"resourceTemplates": RESOURCE_TEMPLATES}
        if method == "resources/read":
            return self._read(params)
        if method == "prompts/list":
            return {"prompts": []}
        raise _Rpc(-32601, f"Method not found: {method}")

    # -- handlers --------------------------------------------------------
    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        self.protocol_version = requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION
        self.client_info = params.get("clientInfo") or {}
        instructions = INSTRUCTIONS
        try:
            self.client.initialize()
            principal = self.client.principal or {}
            if principal.get("workspace_id"):
                instructions += (f" Connected to workspace {principal['workspace_id']} "
                                 f"as role {principal.get('role', '?')}.")
        except (McpError, PlatformError) as exc:
            # The handshake still completes: hosts treat a failed initialize as a
            # dead server, while a tool call can carry a readable error.
            self._upstream_note = str(exc)
            log.warning("platform not reachable at initialize: %s", exc)
            instructions += f" (The platform could not be reached when this bridge started: {exc})"
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
            },
            "serverInfo": {"name": self.name, "version": self.version},
            "instructions": instructions,
        }

    def _descriptor(self, tool: dict) -> dict:
        name = tool.get("name", "")
        out = {
            "name": name,
            "description": tool.get("description", ""),
            "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}},
            "annotations": {
                "readOnlyHint": name in READ_ONLY_TOOLS,
                "destructiveHint": False,
                "idempotentHint": name in READ_ONLY_TOOLS,
                "openWorldHint": False,
            },
        }
        if tool.get("title"):
            out["title"] = tool["title"]
        return out

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise _Rpc(-32602, "tools/call requires `name`")
        if not isinstance(arguments, dict):
            raise _Rpc(-32602, "tools/call `arguments` must be an object")
        if name not in {t.get("name") for t in self.client.tools()}:
            raise _Rpc(-32602, f"Unknown tool: {name}")
        try:
            raw = self.client.call_raw(name, arguments)
        except McpError as exc:
            # An application error from the platform is a tool failure the model
            # should read, not a protocol failure that ends the session.
            text = str(exc) + (f": {exc.meaning}" if exc.meaning else "")
            return {"content": [{"type": "text", "text": text}], "isError": True}
        data = unwrap(raw)
        result: dict = {
            "content": [{"type": "text", "text": json.dumps(data, indent=2, sort_keys=True, default=str)}],
            "isError": bool(raw.get("isError")),
        }
        if isinstance(data, dict):
            result["structuredContent"] = data
        return result

    def _read(self, params: dict) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise _Rpc(-32602, "resources/read requires `uri`")
        try:
            raw = self.client.read_raw(uri)
        except McpError as exc:
            if exc.code == -32602:
                raise _Rpc(-32002, f"Resource not found: {uri}")
            raise
        contents = []
        for block in raw.get("contents") or []:
            contents.append({"uri": block.get("uri") or uri, "mimeType": block.get("mimeType") or "application/json",
                             "text": block.get("text") or ""})
        return {"contents": contents}


class _Rpc(Exception):
    def __init__(self, code: int, message: str, data: dict | None = None):
        self.code, self.message, self.data = code, message, data
        super().__init__(message)


def _result(jid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": jid, "result": result}


def _error(jid: Any, code: int, message: str, data: dict | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": jid, "error": err}
