"""The HTTP client every dmz command uses: one API key against one platform
endpoint, requests paced under the vendor rate limit and a 429 waited out.
A small HTTP client for the DMZAgent operator surface.

Standard library only. The toolkit talks to the same `/v1` endpoints the
console and the SDKs use, authenticated as an API key. Two behaviours matter
for a tool that reconciles a whole governance declaration in one run:

* The platform rate-limits per vendor (free tier: 10 requests a second,
  120 a minute). A 429 carries `Retry-After`; this client waits and retries
  instead of failing the run halfway through an apply.
* Every request is paced to stay under the burst limit in the first place.

The `transport` is injectable so tests exercise real request shaping against
a fake server.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DEFAULT_BASE_URL = "https://api.dmzagent.com"
USER_AGENT = "dmz/0.1.0"

Transport = Callable[[str, str, dict, bytes | None, float], tuple[int, dict, bytes]]


class PlatformError(RuntimeError):
    """A request the platform refused. Carries the status and the detail."""

    def __init__(self, status: int, detail: Any, method: str, path: str):
        self.status = status
        self.detail = detail
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} -> {status}: {_short(detail)}")


class ConfigError(RuntimeError):
    """The toolkit was not configured well enough to talk to the platform."""


def _short(detail: Any, limit: int = 300) -> str:
    text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None,
                      timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()
    except urllib.error.URLError as exc:
        raise ConfigError(f"could not reach {url}: {exc.reason}") from exc


def fingerprint(api_key: str) -> str:
    """A safe way to name a key in output: its prefix, never the secret."""
    return api_key[:8] + "…" if api_key else "(none)"


class Platform:
    """One DMZAgent deployment, as one API key."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, *,
                 transport: Transport | None = None, min_interval: float = 0.125,
                 max_retries: int = 6, timeout: float = 90.0,
                 log: Callable[[str], None] | None = None):
        key = (api_key or os.environ.get("DMZAGENT_API_KEY") or "").strip()
        if not key.startswith("ck_"):
            raise ConfigError(
                "DMZAGENT_API_KEY is missing or is not a DMZAgent key (keys start "
                "with ck_). Mint a tenant_admin key in the console under Team & access.")
        self.api_key = key
        self.base_url = (base_url or os.environ.get("DMZAGENT_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport or _urllib_transport
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._timeout = timeout
        self._log = log or (lambda msg: None)
        self._last_request = 0.0
        self.requests_made = 0
        self.seconds_waited = 0.0

    # ----------------------------------------------------------------- #

    def request(self, method: str, path: str, body: Any = None, *,
                query: dict | None = None, headers: dict | None = None) -> tuple[int, Any]:
        """Perform one request, honouring pacing and 429 retries. Returns
        (status, parsed body). Raises PlatformError for 4xx/5xx other than a
        retried 429."""
        url = self.base_url + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += ("&" if "?" in path else "?") + urllib.parse.urlencode(clean)
        hdrs = {"authorization": f"Bearer {self.api_key}", "accept": "application/json",
                "user-agent": USER_AGENT}
        if headers:
            hdrs.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["content-type"] = "application/json"

        for attempt in range(self._max_retries + 1):
            self._pace()
            status, resp_headers, raw = self._transport(method, url, hdrs, data, self._timeout)
            self.requests_made += 1
            parsed = _parse(raw)
            if status == 429 and attempt < self._max_retries:
                wait = _retry_after(resp_headers, parsed)
                self._log(f"rate limited on {method} {path}; waiting {wait:.1f}s")
                self.seconds_waited += wait
                time.sleep(wait)
                continue
            if status >= 500 and attempt < 2:
                wait = 1.5 * (attempt + 1)
                self._log(f"{status} on {method} {path}; retrying in {wait:.1f}s")
                time.sleep(wait)
                continue
            if status >= 400:
                detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
                raise PlatformError(status, detail, method, path)
            return status, parsed
        raise PlatformError(429, "rate limit did not clear", method, path)

    def get(self, path: str, **query: Any) -> Any:
        return self.request("GET", path, query=query or None)[1]

    def post(self, path: str, body: Any = None, **query: Any) -> Any:
        return self.request("POST", path, body if body is not None else {}, query=query or None)[1]

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body)[1]

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, body)[1]

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)[1]

    # ----------------------------------------------------------------- #

    def rpc(self, method: str, params: dict | None = None) -> Any:
        """One JSON-RPC call to the Concordia MCP surface (`/mcp/v1`)."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        _, data = self.request("POST", "/mcp/v1", body)
        if isinstance(data, dict) and data.get("error"):
            raise PlatformError(200, data["error"], "MCP", method)
        return (data or {}).get("result")

    def whoami(self) -> dict:
        """The principal behind the key: its workspace and role."""
        me = self.get("/v1/auth/me")
        if not me.get("authenticated"):
            raise PlatformError(401, "the key did not authenticate", "GET", "/v1/auth/me")
        user = me.get("user") or {}
        roles = user.get("workspace_roles") or {}
        if len(roles) != 1:
            raise ConfigError(
                "expected the key to be bound to exactly one workspace; "
                f"the platform reports {sorted(roles)}")
        workspace_id, role = next(iter(roles.items()))
        return {"user_id": user.get("user_id"), "vendor_id": user.get("vendor_id"),
                "workspace_id": workspace_id, "role": role, "kind": user.get("kind")}

    def division_id_for_key(self) -> str:
        """The division the key's workspace belongs to.

        The platform does not expose a division lookup to API keys (the
        workspace and division listings are console-only). The client-runner
        handshake does report it, so open and immediately close a runner
        session. DMZAGENT_DIVISION_ID skips the round trip.
        """
        configured = (os.environ.get("DMZAGENT_DIVISION_ID") or "").strip()
        if configured:
            return configured
        sess = self.post("/v1/agent-sessions", {"label": "dmz: division lookup",
                                                "runner": "dmz"})
        division_id = sess.get("division_id")
        session_id = sess.get("session_id")
        if session_id:
            try:
                self.delete(f"/v1/agent-sessions/{session_id}")
            except PlatformError:
                pass
        if not division_id:
            raise ConfigError(
                "could not determine the key's division; set DMZAGENT_DIVISION_ID "
                "(the console shows it on the division page)")
        return division_id

    # ----------------------------------------------------------------- #

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()


def _parse(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return raw.decode("utf-8", "replace")


def _retry_after(headers: dict, body: Any) -> float:
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    candidates = [lowered.get("retry-after")]
    if isinstance(body, dict):
        candidates.append(body.get("retry_after"))
    for c in candidates:
        try:
            if c is not None:
                return max(0.2, float(c)) + 0.1
        except (TypeError, ValueError):
            continue
    return 1.1
