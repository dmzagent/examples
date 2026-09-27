# 03 · Your own chatbot on Ollama, governed by the SDK

Harbor Supply runs its own assistant on an open-weights model through
[Ollama](https://ollama.com), with three tools: look up an order, issue a
refund, export customer records. The DMZAgent SDK is embedded in the app:
every turn goes to the platform, and before a **governed** tool runs the
assistant's circuit breaker is checked. The page shows what the platform
saw, what it tagged, and what it decided, next to the chat.

```
governance.py    the OWASP LLM Top 10 vocabulary, breaker rules, response
                 lanes, and the application key
setup.sh         plan → apply → verify; installs the SDK
app/chat.py      the assistant: Ollama for the model, the SDK for governance
app/site/        the chat page with the "what the platform saw" panel
```

## Run it

```bash
ollama pull llama3.1                 # any tool-capable model; set OLLAMA_MODEL to use another
export DMZAGENT_API_KEY=ck_...       # a tenant_admin key for the workspace
./setup.sh                           # writes app/.env with an application key
python3 app/chat.py                  # http://localhost:8001
```

`OLLAMA_URL` points the app at a remote Ollama (default `http://localhost:11434`).

## What happens on a turn

1. The customer's message is sent as `says()` on the conversation handle.
   The platform answers with a frame id at once; the app waits for the
   reasoning outcome in the background and shows the tags when they land.
2. Ollama answers, possibly with tool calls. Every tool call and result is
   recorded (`tool_call()`, `tool_result()`).
3. Before `issue_refund` or `export_customer_records` runs, `check()` reads
   the assistant's breaker:
   - `closed` runs the tool;
   - `half_open` runs it and marks the result flagged;
   - `hold` or `open` refuses it. The refusal goes back to the model as the
     tool's result, so the assistant tells the customer a person will follow
     up instead of pretending.
4. The assistant's reply is recorded with `says()` too, so the platform
   reasons over what the assistant said, not only what it was asked.

`lookup_order` is not governed; it is still on the record.

## What the governance declares

| Declaration | Effect |
|---|---|
| `require_canon("cn_owasp_llm_top10")` | the OWASP LLM Top 10 (2025) as tags: prompt injection, sensitive disclosure, excessive agency, improper output handling |
| breaker `block` on sensitive information disclosure | disclosure is not recoverable, so it blocks rather than holds |
| breaker `review` on suspected prompt injection | half-open: allowed, flagged on every check |
| `enforce/hold` on strong prompt injection | a hold costs a review, not a failed request; a person releases it |
| `coordinate/escalate` on excessive agency | a person is told; scope is a problem to fix, not a single action to stop |
| `record` on improper output handling | written down, nothing interrupted: the posture to learn your rate on real traffic |
| `coordinate/review` on escalation | core vocabulary; works before any Canon is installed |
| `sdk_key(...)` | the application's analyst key |

The panel's two buttons hold and release the assistant as operator overrides
recorded on the ledger. Hold it, ask for a refund, and watch the refusal
reach the model.

## Honest limits

- `check()` reads cached breaker state from earlier reasoning. A refusal
  means the assistant already tripped a policy; the message being sent now
  is judged by the reasoning that follows it, not before it.
- Reasoning runs on the platform. On a deployment without inference the
  outcome column reads `reasoning…` and then `unknown`; the breaker and the
  overrides still work, and so does the chat.
- Whether the model calls a tool at all is up to the model. Use a
  tool-capable model; the fake Ollama in the tests is scripted.

## Tests

```
pip install -r requirements.txt
python3 -m unittest discover -s tests
```

A fake Ollama and a fake platform on localhost; the real SDK in between.
Covers the event sequence of a turn, the check before a governed tool in
each breaker state, the refusal reaching the model, and outcomes arriving
with their tags.
