"""Harbor Supply's own assistant: Ollama answers, the DMZAgent SDK governs.

Every turn goes to the platform through the SDK's conversation handle — what
the customer said, what the assistant replied, and each tool the assistant
called with its result. Before a sensitive tool runs, the assistant's
circuit breaker is checked: closed runs it, half-open runs it flagged, hold
or open refuses it and the model is told so. After each customer turn the
app waits, in the background, for the platform's reasoning outcome and shows
the tags it fired.

    python3 app/chat.py            # http://localhost:8001
    OLLAMA_MODEL=qwen3 python3 app/chat.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import queue

from dmzagent import DMZAgent, DMZAgentError, RateLimitError, subject_id_for_division

HERE = Path(__file__).resolve().parent
SUBJECT_TYPE = "chat"
SENSITIVE_TOOLS = {"issue_refund", "export_customer_records"}

SYSTEM_PROMPT = (
    "You are the support assistant for Harbor Supply, a marine hardware store. "
    "Help with orders, refunds and product questions using the tools you have. "
    "Refunds and record exports are governed: if a tool answers that it was "
    "refused, tell the customer a person will follow up, and do not try again. "
    "Never repeat a customer's card number back to them."
)

ORDERS = {
    "HS-1001": {"customer": "Dana Reyes", "items": ["dock line 40m", "stainless thimbles x12"],
                "total": 412.50, "status": "delivered", "card_last4": "4471"},
    "HS-1002": {"customer": "Priya Nair", "items": ["A4 hex bolts x2000"], "total": 1880.00,
                "status": "shipped", "card_last4": "9023"},
    "HS-1003": {"customer": "Tom Okafor", "items": ["injector pump repair"], "total": 965.00,
                "status": "processing", "card_last4": "1187"},
}

TOOLS = [
    {"type": "function", "function": {
        "name": "lookup_order",
        "description": "Look up an order by id (HS-1001 style). Returns status, items and total.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                       "required": ["order_id"]}}},
    {"type": "function", "function": {
        "name": "issue_refund",
        "description": "Refund an amount to the customer's original payment method. Governed.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"},
                                                        "amount": {"type": "number"},
                                                        "reason": {"type": "string"}},
                       "required": ["order_id", "amount"]}}},
    {"type": "function", "function": {
        "name": "export_customer_records",
        "description": "Export customer records matching a filter as a file. Governed.",
        "parameters": {"type": "object", "properties": {"filter": {"type": "string"}},
                       "required": ["filter"]}}},
]


def run_tool(name: str, args: dict) -> dict:
    if name == "lookup_order":
        order = ORDERS.get(str(args.get("order_id", "")).upper())
        if not order:
            return {"error": "no such order"}
        return {k: v for k, v in order.items() if k != "card_last4"}
    if name == "issue_refund":
        order_id = str(args.get("order_id", "")).upper()
        if order_id not in ORDERS:
            return {"error": "no such order"}
        return {"refunded": float(args.get("amount") or 0), "order_id": order_id, "reference": f"RF-{int(time.time())}"}
    if name == "export_customer_records":
        return {"exported": len(ORDERS), "file": "customers.csv"}
    return {"error": f"unknown tool {name}"}


class Pacer:
    """One gate for every platform call the app makes, from any thread.

    The platform rate-limits per vendor (free tier: 10 requests a second,
    120 a minute). Spacing calls out and waiting a 429's `retry_after`
    once keeps a chat turn — four events, a check, and the background
    outcome poll — inside that budget instead of failing halfway.
    """

    def __init__(self, min_interval: float = 0.15):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

    def call(self, fn):
        """Run one SDK call; on a 429, wait what the platform asks and retry once."""
        for attempt in range(2):
            self.wait()
            try:
                return fn()
            except RateLimitError as exc:
                if attempt == 1:
                    raise
                time.sleep((exc.retry_after or 1) + 0.1)
        raise AssertionError("unreachable")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                values[k.strip()] = json.loads(v) if v.startswith('"') else v
    return values


class Ollama:
    """The model. Standard REST calls to a local Ollama server."""

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())

    def models(self) -> list[str]:
        with urllib.request.urlopen(self.base_url + "/api/tags", timeout=10) as resp:
            return [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        return self._post("/api/chat", {"model": self.model, "messages": messages,
                                        "tools": tools, "stream": False}).get("message") or {}


class GovernedAssistant:
    """One assistant subject; one SDK conversation per browser conversation."""

    def __init__(self, cfg: dict[str, str], ollama: Ollama, dmz: DMZAgent):
        self.cfg = cfg
        self.ollama = ollama
        self.dmz = dmz
        division = cfg["DMZAGENT_DIVISION_ID"]
        self.bot = subject_id_for_division(division, cfg.get("BOT_SUBJECT_SLUG", "harbor-assistant"),
                                           subject_type=SUBJECT_TYPE)
        self.division = division
        self.conversations: dict[str, dict] = {}
        self.turns: list[dict] = []
        self.lock = threading.Lock()
        # What a governed tool does when the breaker cannot be checked at all:
        # "refuse" (default) or "allow". A refund that runs because the
        # governance call failed is the failure this setting exists to name.
        self.on_error = cfg.get("GOVERNANCE_ON_ERROR", "refuse")
        # Outcomes are polled one frame at a time. The platform rate-limits
        # per vendor (free tier: 10 requests a second) and a poll per frame in
        # parallel would spend that budget on waiting instead of on turns.
        self._outcomes: "queue.Queue[dict]" = queue.Queue()
        self.pacer = Pacer()
        threading.Thread(target=self._outcome_worker, daemon=True).start()

    # --- conversations ------------------------------------------------- #

    def conversation(self, conversation_id: str) -> dict:
        with self.lock:
            conv = self.conversations.get(conversation_id)
            if conv:
                return conv
            customer = subject_id_for_division(self.division, f"visitor-{conversation_id}",
                                               subject_type=SUBJECT_TYPE)
            handle = self.dmz.conversation(participants=[
                {"subject_id": self.bot, "role": "agent", "kind": "agent"},
                {"subject_id": customer, "role": "customer", "kind": "human"},
            ])
            conv = {"id": conversation_id, "customer": customer, "handle": handle,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}]}
            self.conversations[conversation_id] = conv
            return conv

    def _record(self, kind: str, fn, **fields) -> dict:
        """Emit one event through the SDK; never let a governance hiccup
        take the chat down, but always say what happened."""
        entry = {"kind": kind, "at": time.strftime("%H:%M:%S"), **fields}
        try:
            r = self.pacer.call(fn)
            entry.update({"frame_id": r.frame_id, "accepted": r.accepted, "outcome": None, "tags": []})
            if r.frame_id and kind in ("customer", "assistant"):
                self._outcomes.put(entry)
        except DMZAgentError as exc:
            entry.update({"frame_id": None, "accepted": False, "error": str(exc)})
        with self.lock:
            self.turns.append(entry)
            del self.turns[:-40]
        return entry

    def _outcome_worker(self) -> None:
        while True:
            entry = self._outcomes.get()
            self._await(entry)

    def _await(self, entry: dict, *, timeout: float = 45.0, every: float = 3.0) -> None:
        """Poll the frame's story until every workspace has reasoned, a few
        seconds apart. The SDK's await_outcome() polls from 100 ms upward,
        which is right for one frame and wrong for a chat that produces four
        per turn on a rate-limited key."""
        deadline = time.monotonic() + timeout
        while True:
            story = self._rest("GET", f"/v1/frames/{entry['frame_id']}/story")
            summary = story.get("summary") or {}
            if summary.get("complete"):
                entry["outcome"] = story.get("outcome") or "no_change"
                entry["tags"] = [{"tag": t.get("tag_id") or t.get("name"), "strength": t.get("strength")}
                                 for t in story.get("tags_fired") or []]
                entry["complete"] = True
                return
            if "error" in story and story.get("error") not in (None, 0):
                entry["outcome"] = "unknown"
                entry["error"] = str(story.get("detail"))[:160]
                return
            if time.monotonic() >= deadline:
                entry["outcome"] = "unknown"
                entry["error"] = "reasoning did not complete in time"
                return
            time.sleep(every)

    # --- a turn --------------------------------------------------------- #

    def turn(self, conversation_id: str, text: str) -> dict:
        conv = self.conversation(conversation_id)
        handle = conv["handle"]
        self._record("customer", lambda: handle.says(conv["customer"], SUBJECT_TYPE, text), text=text)
        conv["messages"].append({"role": "user", "content": text})

        events: list[dict] = []
        last_check: dict | None = None
        reply = ""
        for _round in range(4):
            message = self.ollama.chat(conv["messages"], TOOLS)
            calls = message.get("tool_calls") or []
            if not calls:
                reply = (message.get("content") or "").strip()
                break
            conv["messages"].append({"role": "assistant", "content": message.get("content") or "",
                                     "tool_calls": calls})
            for call in calls:
                fn = call.get("function") or {}
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"raw": args}
                event = {"tool": name, "args": args, "governed": name in SENSITIVE_TOOLS}
                self._record("tool_call", lambda: handle.tool_call(self.bot, SUBJECT_TYPE, name, args),
                             tool=name, args=args)
                if name in SENSITIVE_TOOLS:
                    last_check = self.check_breaker()
                    event["governance"] = last_check
                    if not last_check["allow"]:
                        result = {"refused": True, "reason": last_check["reason"],
                                  "note": "a person will follow up" if last_check.get("held") else "refused"}
                    else:
                        result = run_tool(name, args)
                        if last_check.get("warning"):
                            result["flagged"] = last_check["reason"]
                else:
                    result = run_tool(name, args)
                event["result"] = result
                events.append(event)
                self._record("tool_result", lambda: handle.tool_result(self.bot, SUBJECT_TYPE, name, result),
                             tool=name, result=result)
                conv["messages"].append({"role": "tool", "tool_name": name, "content": json.dumps(result)})
        else:
            reply = "I have done what I can on this; a person will follow up."

        if not reply:
            reply = "I am not sure how to help with that. Could you say more?"
        conv["messages"].append({"role": "assistant", "content": reply})
        self._record("assistant", lambda: handle.says(self.bot, SUBJECT_TYPE, reply), text=reply)
        return {"reply": reply, "events": events, "governance": last_check}

    def check_breaker(self) -> dict:
        """The assistant's breaker, as a plain dict. A 429 is waited out once;
        any other failure applies the on-error posture rather than guessing."""
        try:
            check = self.pacer.call(lambda: self.dmz.check(subject_id=self.bot))
            return {"state": check.state, "allow": check.allow, "warning": check.warning,
                    "held": bool(check.raw.get("held")), "reason": check.reason}
        except DMZAgentError as exc:
            error = str(exc)[:160]
        allow = self.on_error == "allow"
        return {"state": "unknown", "allow": allow, "warning": True, "held": False, "unavailable": True,
                "reason": f"breaker could not be checked ({error}); "
                          + ("allowed by GOVERNANCE_ON_ERROR=allow" if allow else "refused until it can be")}

    # --- the panel ------------------------------------------------------- #

    def state(self) -> dict:
        try:
            check = self.pacer.call(lambda: self.dmz.check(subject_id=self.bot))
            breaker = {"state": check.state, "allow": check.allow, "warning": check.warning,
                       "held": bool(check.raw.get("held")), "reason": check.reason,
                       "fired_policies": check.fired_policies, "anchor": check.anchor}
        except DMZAgentError as exc:
            breaker = {"state": "unknown", "reason": str(exc)[:200]}
        with self.lock:
            turns = list(self.turns)[-20:]
        return {"bot": self.bot, "breaker": breaker, "turns": turns,
                "reviews": self._rest("GET", "/v1/reviews", workspace_id=self.cfg["DMZAGENT_WORKSPACE_ID"]).get("reviews", []),
                "decisions": self._rest("GET", "/v1/cb/decisions", workspace_id=self.cfg["DMZAGENT_WORKSPACE_ID"],
                                        scope_ref=self.bot, limit=8).get("decisions", [])}

    def override(self, action: str, reason: str) -> dict:
        return self._rest("POST", f"/v1/cb/{action}", body={"workspace_id": self.cfg["DMZAGENT_WORKSPACE_ID"],
                                                             "subject_id": self.bot, "reason": reason})

    def _rest(self, method: str, path: str, body: dict | None = None, **query) -> dict:
        """The two reads the SDK has no method for (reviews, decisions) and
        the operator overrides, on the same key."""
        url = self.cfg["DMZAGENT_BASE_URL"].rstrip("/") + path
        clean = {k: v for k, v in query.items() if v}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method,
                                     headers={"authorization": f"Bearer {self.cfg['DMZAGENT_APP_KEY']}",
                                              "content-type": "application/json", "accept": "application/json"})
        for attempt in range(2):
            self.pacer.wait()
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 429 and attempt == 0:
                    try:
                        wait = float(exc.headers.get("Retry-After") or json.loads(raw).get("retry_after") or 1)
                    except ValueError:
                        wait = 1.0
                    time.sleep(wait + 0.1)
                    continue
                return {"error": exc.code, "detail": raw[:200]}
            except urllib.error.URLError as exc:
                return {"error": 0, "detail": str(exc.reason)}
        return {"error": 429, "detail": "rate limited"}


def make_handler(assistant: GovernedAssistant):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.command, self.path))

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                html = (HERE / "site" / "index.html").read_text().replace("{{BOT}}", assistant.bot)
                self._send(200, html.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(200, assistant.state())
            elif self.path == "/healthz":
                self._json(200, {"ok": True, "model": assistant.ollama.model})
            else:
                self._json(404, {"detail": "not found"})

        def do_POST(self):
            body = self._body()
            if self.path == "/api/chat":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json(400, {"detail": "text required"})
                try:
                    self._json(200, assistant.turn(body.get("conversation_id") or "default", text))
                except (urllib.error.URLError, OSError) as exc:
                    self._json(502, {"detail": f"Ollama is not answering at {assistant.ollama.base_url}: {exc}"})
            elif self.path in ("/api/drill/hold", "/api/drill/release"):
                self._json(200, assistant.override(self.path.rsplit("/", 1)[1], body.get("reason") or "from the demo panel"))
            else:
                self._json(404, {"detail": "not found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg = load_env(HERE / ".env")
    missing = [k for k in ("DMZAGENT_APP_KEY", "DMZAGENT_WORKSPACE_ID", "DMZAGENT_DIVISION_ID") if not cfg.get(k)]
    if missing:
        print(f"app/.env is missing {', '.join(missing)} — run ./setup.sh first", file=sys.stderr)
        return 2
    cfg.setdefault("DMZAGENT_BASE_URL", "https://api.dmzagent.com")
    ollama = Ollama(os.environ.get("OLLAMA_URL", "http://localhost:11434"),
                    os.environ.get("OLLAMA_MODEL", "llama3.1"))
    try:
        names = ollama.models()
        if not any(n == ollama.model or n.startswith(ollama.model + ":") for n in names):
            print(f"Ollama has no model {ollama.model!r} (has {names or 'none'}). Run: ollama pull {ollama.model}",
                  file=sys.stderr)
    except (urllib.error.URLError, OSError) as exc:
        print(f"warning: Ollama not reachable at {ollama.base_url} ({exc}); start it with `ollama serve`",
              file=sys.stderr)
    dmz = DMZAgent(api_key=cfg["DMZAGENT_APP_KEY"], base_url=cfg["DMZAGENT_BASE_URL"], timeout=20.0)
    assistant = GovernedAssistant(cfg, ollama, dmz)
    port = int(os.environ.get("PORT") or (argv[0] if argv else 8001))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(assistant))
    print(f"Harbor Supply assistant: http://localhost:{port}  (model {ollama.model}; subject {assistant.bot})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
