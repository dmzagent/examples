"""Harbor Supply's back-office agent, governed through the platform's MCP server.

    python3 app/agent.py --script        # the scripted day, in the terminal
    python3 app/agent.py                 # a conversation; Ctrl-D ends it

The model runs on Ollama. Its tools are the platform's MCP tools, discovered
with tools/list at start, plus three of Harbor Supply's own: look up an
order, issue a refund, export a customer's records. The system prompt tells
the model to call enforce_covenant before a refund or an export and
record_decision after. The harness does not trust it to: before a governed
tool runs, the harness checks that a pre-flight verdict of `allow` exists
for that customer and action in this turn, runs the pre-flight itself when
the model skipped it, refuses the tool on any other verdict, and, when the
turn ends without the model recording what it did, records it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "cli"))

from dmz.mcp.client import McpClient, McpError, PlatformError  # noqa: E402

# -- Harbor Supply's own tools ---------------------------------------------

ORDERS = {
    "HS-1001": {"customer": "customer:alice", "item": "Deck chair (teak)", "total": 50.0, "status": "delivered"},
    "HS-1002": {"customer": "customer:alice", "item": "Mooring line 12mm", "total": 24.0, "status": "shipped"},
    "HS-1003": {"customer": "customer:bob", "item": "Bilge pump", "total": 89.0, "status": "delivered"},
}

LOCAL_TOOLS = [
    {"type": "function", "function": {
        "name": "lookup_order", "description": "Look up an order by id: item, total, status.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}},
    {"type": "function", "function": {
        "name": "issue_refund", "description": "Refund an order. Governed: call enforce_covenant first with action_kind 'issue_refund'.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"},
                                                        "reason": {"type": "string"}}, "required": ["order_id", "amount"]}}},
    {"type": "function", "function": {
        "name": "export_customer_records", "description": "Export every record held about the customer. Governed: call enforce_covenant first with action_kind 'export_records'.",
        "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]}}},
]

# tool name → the action_kind the platform is asked about, and the decision recorded afterwards
GOVERNED = {"issue_refund": ("issue_refund", "refund_issued"),
            "export_customer_records": ("export_records", "records_exported")}


def run_local_tool(name: str, args: dict) -> dict:
    if name == "lookup_order":
        order = ORDERS.get(args.get("order_id", ""))
        return {"order_id": args.get("order_id"), **order} if order else {"error": "no such order"}
    if name == "issue_refund":
        order = ORDERS.get(args.get("order_id", ""))
        if not order:
            return {"error": "no such order"}
        amount = float(args.get("amount") or order["total"])
        return {"ok": True, "order_id": args["order_id"], "refunded": min(amount, order["total"]), "reason": args.get("reason")}
    if name == "export_customer_records":
        cid = args.get("customer_id", "")
        rows = [{"order_id": k, **v} for k, v in ORDERS.items() if v["customer"] == cid]
        return {"ok": True, "customer_id": cid, "records": rows}
    return {"error": f"unknown tool {name}"}


# -- the model ---------------------------------------------------------------

class Ollama:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def models(self) -> list[str]:
        with urllib.request.urlopen(self.base_url + "/api/tags", timeout=10) as resp:
            return [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body = json.dumps({"model": self.model, "messages": messages, "tools": tools, "stream": False}).encode()
        req = urllib.request.Request(self.base_url + "/api/chat", data=body, headers={"content-type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read()).get("message") or {}


# -- the agent ---------------------------------------------------------------

SYSTEM_PROMPT = """You are {agent_name}, the back-office assistant of Harbor Supply, a chandlery.
You are serving the customer whose subject id is "{customer}". Use exactly that value as subject_id
whenever you call enforce_covenant, record_decision or get_subject_soul.

Rules you must follow:
1. Before issue_refund, call enforce_covenant with action_kind "issue_refund" and the order in action_payload.
   Before export_customer_records, call enforce_covenant with action_kind "export_records".
2. Act only when the verdict is "allow". On "review" say a person must approve; on "block" apologise and stop.
3. After a refund or an export, call record_decision (decision_kind "refund_issued" or "records_exported").
4. If a tool is refused, explain the reason to the customer in one sentence. Never invent order details.
Keep replies to one or two sentences."""


class Agent:
    def __init__(self, model: Ollama, mcp: McpClient, *, customer: str, agent_name: str = "Harbor Supply back office",
                 on_event: Callable[[str], None] | None = None):
        self.model = model
        self.mcp = mcp
        self.customer = customer
        self.say = on_event or (lambda line: None)
        self.platform_tools = {t["name"]: t for t in mcp.tools()}
        self.tools = [self._as_ollama_tool(t) for t in self.platform_tools.values()] + LOCAL_TOOLS
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT.format(agent_name=agent_name, customer=customer)}]
        self.ledger: list[str] = []          # every ledger anchor this conversation produced

    @staticmethod
    def _as_ollama_tool(tool: dict) -> dict:
        return {"type": "function", "function": {
            "name": tool["name"], "description": tool.get("description", ""),
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}}}}

    def turn(self, user_text: str, *, max_rounds: int = 8) -> str:
        """One customer message; the model may call tools for several rounds."""
        self.messages.append({"role": "user", "content": user_text})
        verdicts: dict[tuple[str, str], dict] = {}     # (subject, action_kind) → the pre-flight this turn
        recorded: set[str] = set()                     # decision kinds the model recorded itself
        pending: list[tuple[str, dict]] = []           # actions taken, waiting for a record
        for _ in range(max_rounds):
            reply = self.model.chat(self.messages, self.tools)
            calls = reply.get("tool_calls") or []
            if not calls:
                text = reply.get("content") or ""
                self.messages.append({"role": "assistant", "content": text})
                self._settle(pending, recorded)
                return text
            self.messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = call.get("function") or {}
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                result = self._dispatch(name, args, verdicts, recorded, pending)
                self.messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result)})
        text = "I could not finish that; a person will follow up."
        self.messages.append({"role": "assistant", "content": text})
        self._settle(pending, recorded)
        return text

    def _settle(self, pending: list, recorded: set) -> None:
        """The turn is over: anything acted on and not recorded by the model is
        recorded by the harness. The ledger never depends on the model's manners."""
        for decision_kind, args in pending:
            if decision_kind in recorded:
                continue
            self.say(f"  ! the model ended the turn without recording {decision_kind}; the harness records it")
            self._platform("record_decision", {"subject_id": self.customer, "decision_kind": decision_kind,
                                               "payload": args, "actor": "agent", "outcome": "completed"})

    # -- tool dispatch: the platform's tools go over MCP; governed local tools go through the gate

    def _dispatch(self, name: str, args: dict, verdicts: dict, recorded: set, pending: list) -> dict:
        if name in self.platform_tools:
            result = self._platform(name, args)
            if name == "enforce_covenant" and "verdict" in result:
                verdicts[(args.get("subject_id", ""), args.get("action_kind", ""))] = result
            if name == "record_decision" and "ledger_entry_id" in result:
                recorded.add(args.get("decision_kind", ""))
            return result
        if name in GOVERNED:
            return self._governed(name, args, verdicts, pending)
        result = run_local_tool(name, args)
        self.say(f"  ⇢ {name} {_brief(args)} → {_brief(result)}")
        return result

    def _platform(self, name: str, args: dict) -> dict:
        try:
            result = self.mcp.call(name, args)
        except McpError as exc:
            self.say(f"  ✗ {name} {_brief(args)} → {exc}" + (f" ({exc.meaning})" if exc.meaning else ""))
            return {"error": str(exc), "error_id": exc.error_id}
        except PlatformError as exc:
            self.say(f"  ✗ {name} → the platform refused the request: {exc}")
            return {"error": f"platform unavailable: {exc}"}
        if not isinstance(result, dict):
            result = {"result": result}
        if name == "enforce_covenant":
            self.say(f"  ⇢ enforce_covenant subject={args.get('subject_id')} action={args.get('action_kind')} "
                     f"→ {result.get('verdict')}  ({result.get('rationale', '')}; ledger {_short(result.get('ledger_entry_id'))})")
        elif name == "record_decision":
            self.say(f"  ⇢ record_decision {args.get('decision_kind')} {args.get('outcome', 'completed')} "
                     f"→ ledger {_short(result.get('ledger_entry_id'))} chain {_short(result.get('chain_head_hash'))}")
        else:
            self.say(f"  ⇢ {name} {_brief(args)} → {_brief(result)}")
        for key in ("ledger_entry_id",):
            if result.get(key):
                self.ledger.append(result[key])
        return result

    def _governed(self, name: str, args: dict, verdicts: dict, pending: list) -> dict:
        action_kind, decision_kind = GOVERNED[name]
        verdict = verdicts.get((self.customer, action_kind))
        if verdict is None:
            self.say(f"  ! the model called {name} without a pre-flight; the harness runs enforce_covenant")
            verdict = self._platform("enforce_covenant", {"subject_id": self.customer, "action_kind": action_kind,
                                                          "action_payload": args, "context": {"added_by": "harness"}})
            verdicts[(self.customer, action_kind)] = verdict
        if verdict.get("verdict") != "allow":
            reason = verdict.get("rationale") or verdict.get("error") or "the platform did not allow it"
            self.say(f"  ✗ {name} refused: verdict {verdict.get('verdict', 'unknown')} — {reason}")
            self._platform("record_decision", {"subject_id": self.customer, "decision_kind": decision_kind,
                                               "payload": {**args, "verdict": verdict.get("verdict")}, "actor": "agent",
                                               "outcome": "rejected"})
            return {"refused": True, "verdict": verdict.get("verdict"), "reason": reason}
        result = run_local_tool(name, args)
        self.say(f"  ⇢ {name} {_brief(args)} → {'ok' if result.get('ok') else _brief(result)}")
        verdicts.pop((self.customer, action_kind), None)      # one verdict, one action
        pending.append((decision_kind, args))                  # recorded by the model, or by the harness at turn end
        return result


def _brief(value, limit: int = 70) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "…"


def _short(value) -> str:
    return str(value or "")[:8]


# -- the scripted day and the terminal --------------------------------------

SCRIPT = [
    "What's the status of order HS-1002?",
    "Order HS-1001 arrived broken. Please refund it.",
    "Export all the records you hold about me.",
]


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Harbor Supply's agent on the platform's MCP server")
    p.add_argument("--script", action="store_true", help="play the scripted day instead of a conversation")
    p.add_argument("--customer", default="customer:alice", help="the subject the conversation is about")
    p.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    p.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "llama3.1"))
    args = p.parse_args(argv)

    env = load_env(HERE / ".env")
    missing = [k for k in ("DMZAGENT_APP_KEY", "DMZAGENT_WORKSPACE_ID") if not env.get(k)]
    if missing:
        print(f"app/.env is missing {', '.join(missing)} — run ./setup.sh first", file=sys.stderr)
        return 2
    model = Ollama(args.ollama_url, args.model)
    try:
        names = model.models()
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"Ollama is not reachable at {args.ollama_url}: {exc}", file=sys.stderr)
        return 2
    if names and not any(n.split(":")[0] == args.model.split(":")[0] for n in names):
        print(f"model {args.model!r} is not pulled; `ollama pull {args.model}` (have: {', '.join(names)})", file=sys.stderr)
        return 2

    mcp = McpClient(env.get("DMZAGENT_BASE_URL", "https://api.dmzagent.com"), env["DMZAGENT_APP_KEY"],
                    client_name="harbor-mcp-agent")
    info = mcp.initialize()
    hint = info.get("principalHint") or {}
    print(f"MCP server {info.get('serverInfo', {}).get('name')} {info.get('serverInfo', {}).get('version')}: "
          f"workspace {hint.get('workspace_id')}, role {hint.get('role')}")
    agent = Agent(model, mcp, customer=args.customer, agent_name=env.get("AGENT_NAME", "Harbor Supply back office"),
                  on_event=print)
    print(f"tools: {', '.join(agent.platform_tools)} + {', '.join(t['function']['name'] for t in LOCAL_TOOLS)}")
    print(f"customer: {args.customer}\n")

    def exchange(text: str) -> None:
        print(f"you>   {text}")
        reply = agent.turn(text)
        print(f"agent> {reply}\n")

    if args.script:
        for line in SCRIPT:
            exchange(line)
        print(f"{len(agent.ledger)} ledger entries were written for this conversation.")
        print("Hold the customer with `dmz hold customer:alice --reason ...` and play it again.")
        return 0
    print("type a message; Ctrl-D ends the conversation")
    try:
        while True:
            text = input("you>   ").strip()
            if text:
                reply = agent.turn(text)
                print(f"agent> {reply}\n")
    except (EOFError, KeyboardInterrupt):
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
