# Tutorial: your first end-to-end trace

You will send one question through identity, policy, guardrails, the LLM gateway, a policy-checked tool call, and output
guardrails, then open the resulting trace in Jaeger and see every hop as a span. Three commands after the stack is up.

## What you need

- Docker Desktop with about 12 GB memory available.
- `OPENAI_API_KEY` in `infra-designs/poc/.env` (copy `.env.example`). OpenRouter is optional; this tutorial forces `gpt-4o-mini`.
- The stack running and seeded:

```bash
cd infra-designs/poc
make up      # first start downloads the injection classifier (~0.7 GB); wait for "healthy"
make seed    # schema, LiteLLM virtual keys, registry -> OPA, embeds 10 knowledge docs
```

## Step 1: get a token for a Finance user of tenant A

```bash
TOKEN=$(curl -s -X POST localhost:18080/auth/token -H 'Content-Type: application/json' \
  -d '{"user":"finance.a@charity-a.test"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
echo $TOKEN | cut -c1-40
```

You get an RS256 JWT issued by `raisin-api` with claims `tenant_id=tenant-a`, `role=Finance`, 15-minute expiry. In
production this comes from Entra ID; here `raisin-api` stands in and publishes a JWKS the runtime verifies against.

## Step 2: ask the assistant a question about your own tenant's data

```bash
curl -s -X POST localhost:18080/assistant -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"Why was donation 873928 declined?","model":"gpt-4o-mini","temperature":0}' | python3 -m json.tool
```

Real output (2026-09-07, `DEBUG_PANEL=on`):

```json
{
  "answer": "Donation 873928 was declined due to insufficient funds.",
  "trace_id": "71343ded243c0f5bda29fde82d97bf74",
  "policy": {
    "model": "gpt-4o-mini",
    "allowed_tools": ["get_transaction_analysis", "get_donation", "list_transactions",
                      "get_tenant_donation_summary", "get_donor_profile", "find_donor", "search_kb"],
    "withheld_tools": ["get_my_donations", "resend_receipt"],
    "registry_revision": "b6d21bfcf80e4165",
    "decisions": [
      {"stage": "model", "allow": true, "reason": "ok", "model": "gpt-4o-mini"},
      {"stage": "tool", "tool": "get_donation", "allow": true, "reason": "ok"}
    ]
  },
  "tool_calls": [{"tool": "get_donation", "args": {"donation_id": 873928}, "outcome": "ok"}],
  "guardrails": {"input": {"placeholders": 0}, "output": {"verdict": "pass", "presidio_block": 0, "...": "..."}}
}
```

What happened, in order: OPA allowed the model and handed the runtime 7 tool schemas (2 withheld for this role). Presidio
found no PII in the question. The guardrails service (NeMo judge + injection classifier) passed it. The model asked for
`get_donation(873928)`. The runtime looked up the donation's owning tenant, asked OPA again (tool stage, tenant match),
called `raisin-api` with your JWT and the runtime's service token, got a minimized record back, and the model wrote the
answer. Presidio and the guardrails output scan passed the draft.

## Step 3: open the trace

```bash
open http://localhost:16686/trace/71343ded243c0f5bda29fde82d97bf74   # replace with the trace_id from your step 2 response
```

The waterfall for that request (noise spans removed) looked like this:

```
raisin-api      POST /assistant
  agent-runtime   assistant.run
    policy.decide                 stage=model  allow  tools.allowed=[7]  tools.withheld=[2]
    guardrail.input               pass  placeholders=0
      guardrail.presidio.analyze  entities=[]
      guardrail.scan.prompt       pass  nemo=passed  risk=0
        guardrails  guardrail.nemo        pass   (litellm span nested: gpt-4o-mini judge, 469 in / 1 out tokens)
        guardrails  guardrail.classifier  pass   risk=0
    chat gpt-4o-mini              594 in / 18 out  tool.calls=[get_donation]
      litellm  Received Proxy Server Request ... litellm_request
    execute_tool get_donation     tool.result=ok
      pip.donation_tenant         donation.id=873928   (raisin-api db.query)
      policy.decide               stage=tool  allow
      (raisin-api GET /api/donations/873928: db.query x2)
    chat gpt-4o-mini              719 in / 12 out
    guardrail.output              pass
      guardrail.presidio.analyze  entities=[]
      guardrail.scan.output       pass
        guardrails  guardrail.nemo        pass
        guardrails  guardrail.moderation  pass  (litellm /v1/moderations)
```

## What you built

One request, one trace, six services, two policy decisions, three guardrail stages, two model calls and one tool call,
all correlated by a single `trace_id` that the API also returns to the caller. Nothing in the trace contains the
question text or the tool result (only lengths and hashes) unless `DEBUG_TRACE_CONTENT=on`; the gateway's own spans carry
tokens and cost only (F14, see [05-observability-reference.md](05-observability-reference.md#litellm)).
`python3 scripts/trace_walk.py <trace_id>` prints the waterfall above as text with those attributes per hop.

Next:

- Run the same question as `finance.b@charity-b.test` and watch OPA deny the tool call on tenant mismatch:
  [04-failure-paths.md](04-failure-paths.md#f6-tenant-mismatch-on-a-tool-call-abac-deny).
- See what each span means: [05-observability-reference.md](05-observability-reference.md).
- Run all ten canned scenarios: `make demo` (see [../RUNBOOK.md](../RUNBOOK.md)).
