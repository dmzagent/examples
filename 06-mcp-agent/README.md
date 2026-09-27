# 06 · Your agent on the platform's MCP server

Harbor Supply's back-office agent runs on an open-weights model through
Ollama, in a terminal. It has three tools of its own: look up an order,
issue a refund, export a customer's records. Everything it knows about
governance it learns from the platform's **MCP server**: at start it lists
the server's tools, and the model is told to call `enforce_covenant` before
a refund or an export and `record_decision` after. The harness does not
take the model's word for it. Before a governed tool runs it checks that a
pre-flight verdict of `allow` exists for that customer and that action in
this turn, runs the pre-flight itself when the model skipped it, refuses
the tool on any other verdict, and, when the turn ends without the model
recording what it did, records it.

The same server can sit behind Claude Code, Claude Desktop or Cursor. The
second half of this page attaches it.

```
governance.py    what moves the customer's breaker, and the agent's analyst key
setup.sh         plan → apply → verify, then the host configuration
app/agent.py     the agent: Ollama, the platform's MCP tools, the harness gate
```

## Run it

```bash
export DMZAGENT_API_KEY=ck_...       # tenant_admin, for setup
./setup.sh
ollama pull llama3.1                 # any tool-capable model; OLLAMA_MODEL picks another
python3 app/agent.py --script        # the scripted day
python3 app/agent.py                 # a conversation
```

The scripted day, with the customer's breaker closed:

```
you>   What's the status of order HS-1002?
  ⇢ lookup_order {"order_id": "HS-1002"} → {"order_id": "HS-1002", "customer": "customer:alice", "item": "Mooring …
agent> Order HS-1002 (mooring line) has shipped.

you>   Order HS-1001 arrived broken. Please refund it.
  ⇢ enforce_covenant subject=customer:alice action=issue_refund → allow  (no cached state (default-allow); ledger 5700fa53)
  ⇢ issue_refund {"order_id": "HS-1001", "amount": 50, "reason": "broken"} → ok
  ⇢ record_decision refund_issued completed → ledger 92d5ae71 chain d1f02399
agent> Done: the refund of $50 for HS-1001 is on its way.

you>   Export all the records you hold about me.
  ! the model called export_customer_records without a pre-flight; the harness runs enforce_covenant
  ⇢ enforce_covenant subject=customer:alice action=export_records → allow  (…)
  ⇢ export_customer_records {"customer_id": "customer:alice"} → ok
  ! the model ended the turn without recording records_exported; the harness records it
  ⇢ record_decision records_exported completed → ledger 6dc13f22 chain a91c0e77
agent> Exported 2 records.
```

Now pause the customer from another terminal and play the day again:

```bash
dmz hold customer:alice --reason "chargeback dispute"     # or: python3 -m dmz --env-file app/.env hold ...
python3 app/agent.py --script
```

```
you>   Order HS-1001 arrived broken. Please refund it.
  ⇢ enforce_covenant subject=customer:alice action=issue_refund → block  (manual override by apikey:…: chargeback dispute; ledger 7bc75d11)
agent> I'm sorry, I can't do that right now: the account is under a chargeback dispute.
```

`dmz decisions` shows the transitions, `dmz mcp read concordia:/workspace/recent-ledger`
shows every pre-flight and record with its hash, and `dmz release customer:alice
--reason resolved` lets the agent act again. Every line of that is on the
ledger the platform keeps, not in this process.

## What the server offers

```
dmz mcp tools            # ten tools: two writes every agent makes, eight reads and settings
dmz mcp resources        # policies, installed Canons, the recent ledger
dmz mcp enforce customer:alice issue_refund --payload '{"amount": 50}'   # exit 0 allow, 1 review, 3 block
dmz mcp record customer:alice refund_issued --actor human
```

`enforce_covenant` consults the same breaker `check()` in the SDK consults,
so the verdict an MCP agent gets and the decision an SDK application gets
for the same subject are identical, and both are anchored on the ledger.

## Attach it to Claude Code, Claude Desktop or Cursor

The platform's server speaks JSON-RPC over HTTPS with its own protocol
version ("1.0"). MCP hosts expect the standard handshake, so `dmz mcp bridge`
sits in between: a standard MCP server on stdio that forwards to the
platform with the key you give it. `dmz mcp config` writes the configuration:

```bash
dmz --env-file app/.env mcp config --client claude-code      # a `claude mcp add` command
dmz --env-file app/.env mcp config --client claude-desktop   # JSON for claude_desktop_config.json
dmz --env-file app/.env mcp config --client cursor           # JSON for ~/.cursor/mcp.json
```

The key never appears in the configuration: the bridge reads it from the
env file, or from a profile saved with `dmz auth set`, when the host starts
it. In Claude Code the tools then appear as `mcp__dmzagent__enforce_covenant`
and so on, with read-only ones marked as such, and the server's instructions
tell the model when to call which. Try: "before you email the customer
alice, check with dmzagent whether that is allowed".

## Honest limits

- Four of the ten advertised tools and all three resources work end to end
  today: `enforce_covenant`, `record_decision`, `query_corpus`,
  `get_subject_soul`. The six settings tools (`get_division_config`,
  `get_trace_patterns`, the notification ones) fail with "Tool execution
  failed" on the current server: their handlers take `arguments` while the
  dispatcher passes `args`, and two read a `user_id` the key principal
  does not carry. The agent never needs them; the bridge reports the
  failure as a tool error the model can read.
- The verdict is the breaker's state, so on a deployment without inference
  it moves only by manual override, by the logic engine, or by policies on
  labels that arrived some other way. The drill above uses `dmz hold`.
- `enforce_covenant` writes a ledger entry and meters a `covenant_check`
  every time it is called. That is the point, and also why the harness
  runs it once per action, not once per tool round.

## Tests

```
python3 -m unittest discover -s tests
```

A scripted model and the fake platform's MCP server: a careful model
pre-flights, acts and records; a lazy one gets its pre-flight and its
record from the harness; a held customer is refused and the refusal is
recorded as rejected; an allowed export runs once per verdict; a failing
platform tool is reported to the model, not fatal.
