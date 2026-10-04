# Request lifecycle: hop by hop

Five paths exist in the POC. Two are AI paths (`/assistant`, `/story`), one is the payment path that must never touch AI
(`/donate`), and two are sub-flows inside the AI paths that deserve their own picture (tool authorization, guardrail
layers). Every hop below names the span it emits so you can find it in Jaeger.

## 1. Assistant path (`POST /assistant`)

![Assistant request sequence](../../diagrams/08-poc-assistant-request-sequence.png)

Source: `../../diagrams/08-poc-assistant-request-sequence.mmd`. Real trace: [03-success-paths.md#s1](03-success-paths.md#s1-finance-user-asks-about-own-tenants-donation).

| # | Hop | Container | Span | What can stop the request here |
|---|---|---|---|---|
| 1 | Client posts `{question, model?, temperature?}` with a Bearer JWT | `raisin-api` | `POST /assistant` | 401 missing/invalid token |
| 2 | JWT verified locally (RS256, `aud=aicp`, `iss=raisin-api`) | `raisin-api` | (attributes `enduser.id`, `tenant_id`, `role` on the span) | 401 |
| 3 | Forward to runtime with the same `Authorization` header; httpx injects `traceparent` | `raisin-api` → `agent-runtime` | `POST /run` | runtime unreachable → 5xx |
| 4 | JWT verified again against the JWKS (`/.well-known/jwks.json`, refetched on unknown or rotated kid) | `agent-runtime` | `assistant.run` root, `gen_ai.prompt.name=assistant-system-v1` | 401 |
| 5 | **Model stage.** `POST /v1/data/aicp/authz/decision` with `{stage: model, subject, model, prompt}` | `agent-runtime` → `opa` | `policy.decide` (`policy.stage=model`) | 403 `policy_deny` (`model_not_in_role`, `unapproved_model`, `unapproved_prompt`, `unknown_role`, `unavailable`) |
| 6 | OPA returns `model`, `allowed_tools`, `withheld_tools`, `retrieval_filter`, `registry_revision` | `opa` | (attributes `policy.tools.allowed`, `policy.tools.withheld`, `policy.registry_revision`) | |
| 7 | Pick the LiteLLM virtual key for `(tenant_id, role)`; no mapping means no gateway | `agent-runtime` | | 403 `no_gateway_key` |
| 8 | **Input guardrail (a).** Presidio `/analyze` for PERSON, EMAIL, PHONE, CC at threshold 0.3; runtime applies per-entity thresholds (0.6 / 0.6 / 0.4 / 0.6); matches become `<TYPE_n>` placeholders held in process | `agent-runtime` → `presidio-analyzer` | `guardrail.input` → `guardrail.presidio.analyze` (`guardrail.entities`, `guardrail.placeholders`) | 400 `guardrail_unavailable` |
| 9 | **Input guardrail (b).** `POST /v1/scan/input` with the placeholdered text; guardrails runs NeMo `self check input` (judge `gpt-4o-mini` via LiteLLM), the injection classifier, and regex rules if `RULES_LAYER=on` | `agent-runtime` → `guardrails` → `nemo-guardrails` → `litellm` | `guardrail.scan.prompt` → `guardrail.nemo`, `guardrail.classifier`, (`guardrail.rules`) | 400 `guardrail_block` stage `input` with `reasons` |
| 10 | **Chat.** System prompt `assistant-system-v1` + sanitized question + schemas for `allowed_tools` only; `max_tokens=600` | `agent-runtime` → `litellm` → provider | `chat <model>` (`gen_ai.usage.*`, `gen_ai.tool.calls`, `input.value` hashed) with LiteLLM's `Received Proxy Server Request` nested | 502 `gateway_error` |
| 11 | **Tool loop** (max 4 rounds). For each `tool_call`: `tools.execute` (see section 4) | `agent-runtime` | `execute_tool <name>` | soft: `not_found` / `denied` / `bad_args` returned to the model |
| 12 | Chat again with the tool result appended | `agent-runtime` → `litellm` | `chat <model>` | 502 |
| 13 | **Output guardrail (a).** Presidio on the draft. Any EMAIL / PHONE / CC → refusal text replaces the answer. PERSON → `flagged` | `agent-runtime` → `presidio-analyzer` | `guardrail.output` → `guardrail.presidio.analyze` | soft: 200 with REFUSAL, `verdict=block`, `reason=pii_in_output` |
| 14 | **Output guardrail (b).** `POST /v1/scan/output` `{text, prompt}`: NeMo `self check output`, `omni-moderation-latest` via LiteLLM, regex if enabled | `agent-runtime` → `guardrails` → `nemo-guardrails`, `litellm` | `guardrail.scan.output` → `guardrail.nemo`, `guardrail.moderation` | soft: 200 with REFUSAL, `reason=nemo:self check output` or `moderation:<category>` |
| 15 | **Restore.** Finance / Participant: placeholders restored to originals. Donor: never restored; `presidio-anonymizer` masks PERSON to `<REDACTED>` | `agent-runtime` (→ `presidio-anonymizer` for Donor) | `guardrail.presidio.anonymize` | 400 `guardrail_unavailable` (Donor path) |
| 16 | Envelope: `{answer, trace_id}` always; `policy`, `tool_calls`, `guardrails` only with `DEBUG_PANEL=on` | `agent-runtime` → `raisin-api` → client | | |

## 2. Story path (`POST /story`)

![Story path with PII placeholders](../../diagrams/13-poc-story-pii-sequence.png)

Source: `../../diagrams/13-poc-story-pii-sequence.mmd`. Real output: [03-success-paths.md#s5](03-success-paths.md#s5-participant-story-generator-pii-placeholdered-and-restored).

Same skeleton as the assistant path with four differences:

1. Prompt is `story-generator-v1`; OPA checks it is approved. Participant role has `tools: []`, so no tool schemas and no tool loop.
2. Temperature is fixed at 0.7 (creative text). The model is asked for strict JSON `{story, social_post}`; the runtime parses
   the first `{...}` block and falls back to treating the whole reply as the story if parsing fails.
3. The output stage scans `story + "\n\n" + social_post` together. If it blocks, the response is `{story: REFUSAL, social_post: ""}`.
4. Restoration differs per field. `story` gets every placeholder restored. `social_post` is public: PERSON becomes first name
   only, and any sentence that contains a contact placeholder (phone, email, card) is dropped whole, so the post never reads
   "Contact me at or .".

## 3. Payment path (`POST /donate`), never AI

No JWT (public form). `raisin-api` validates the tenant exists, upserts the donor without overwriting existing contact
fields, takes the donation id from a database sequence (`extra=forbid` rejects a client-supplied id with 422), inserts a
pending donation, calls `payment-gateway /charge` with `{amount, currency, payment_token}` only, records the transaction and
updates the status. The trace contains `raisin-api` and `payment-gateway` spans and `db.query` spans, and nothing else.

`make test` asserts the PCI-scope property on real traces: at least one trace touches `payment-gateway`, at least one has
a `gen_ai.*` attribute, none has both. It also asserts the canary donor's email and phone (used in scenario 0) appear in
zero span attributes or events anywhere.

## 4. Tool call authorization (inside the assistant path)

![Tool call authorization](../../diagrams/10-poc-tool-call-authorization.png)

Source: `../../diagrams/10-poc-tool-call-authorization.mmd`.

The runtime holds no database credentials for the system of record. Every tool is a narrow HTTP call to `raisin-api` with
two credentials: the caller's JWT (who is asking) and the runtime's `X-Service-Token` (proves the call came through the
control plane, so end users cannot hit `/api/*` directly).

Two shapes of tool:

- **By-id tools** (`get_donation`, `get_transaction_analysis`, `get_donor_profile`): the resource has its own owning tenant,
  which may differ from the caller's. Step 1 is a Policy Information Point call, `GET /internal/{kind}s/{id}/tenant`, that
  returns only the tenant. Step 2 is OPA with `resource.tenant_id` set to that owner. Step 3 is the `/api/...` call. Missing id
  (PIP 404), OPA deny, and backend 403/404 all collapse to the same `{"error": "not_found"}` for the model, so a wrong id and
  a foreign id are indistinguishable (no cross-tenant existence oracle). The OPA deny is still a red span with a `policy.deny`
  event and lands in the SIEM feed.
- **Tenant-scoped tools** (`list_transactions`, `get_tenant_donation_summary`, `find_donor`, `get_my_donations`, `search_kb`):
  the resource tenant is the caller's own, so OPA is asked with `resource.tenant_id = subject.tenant_id` and `raisin-api`
  filters by the JWT tenant in SQL. A deny here is returned as `{"error": "denied", "reason": ...}` because there is no
  foreign resource to hide.

`find_donor` adds placeholder resolution: the model passes `<EMAIL_ADDRESS_1>` exactly as it appeared in the prompt; the
runtime maps it back to the real address in process and sends it in a POST body (never a URL, so it never becomes an
`http.url` span attribute). The span records `tool.arg.resolved_placeholder=true`.

`search_kb` is the RAG tool: embed the query through LiteLLM (`embeddings text-embedding-3-small`), vector search in the `kb`
database with the OPA-supplied `retrieval_filter` as the `WHERE` clause, top 4 chunks, then the retrieved-stage guardrail
over each chunk (section 5).

## 5. Guardrail layers

![Guardrail layers](../../diagrams/09-poc-guardrail-layers.png)

Source: `../../diagrams/09-poc-guardrail-layers.mmd`.

| Stage | Where | Layers, in order | Verdict semantics |
|---|---|---|---|
| Input | user text before the model | Presidio analyze → placeholders; then guardrails `/v1/scan/input`: NeMo `self check input` + `injection detection` (YARA: code, sqli, template, xss), classifier (`max(text, sentence)` score ≥ 0.9), rules (opt-in) | any reason → **block**, HTTP 400 |
| Retrieved | each RAG chunk | guardrails `/v1/scan/retrieved`: same layers, but the classifier's whole-text score is authoritative (procedural docs are full of imperative sentences) | any reason → **flagged**, chunk still passed to the model, span + SIEM event |
| Output | model draft before the user | Presidio analyze (EMAIL / PHONE / CC → block; PERSON → flagged); then guardrails `/v1/scan/output`: NeMo `self check output` (sees prompt + response), `omni-moderation-latest` (threshold 0.5), rules (opt-in); then restore or anonymize | any reason → **block**, answer replaced with a fixed refusal, HTTP 200 |

Every hit is named so the span explains itself: `nemo:self check input`, `classifier:INJECTION`, `moderation:violence`,
`rule:tool_coercion`. The guardrails service's `risk_score` is `max(classifier score, 1.0 if NeMo blocked or a rule hit)`.

Why the output stage blocks on raw PII even though the input stage placeholdered it: a tool result or a hallucination can
introduce an email or phone number the user never typed. Anything of those three types that is not one of this request's
placeholders is by definition data the model should not have produced. PERSON is only flagged because names appear
legitimately in stories and are restored from placeholders anyway.

## Related

- Real outputs for each path: [03-success-paths.md](03-success-paths.md), [04-failure-paths.md](04-failure-paths.md)
- Span and attribute names: [05-observability-reference.md](05-observability-reference.md)
- Per-container detail: [containers/agent-runtime.md](containers/agent-runtime.md), [containers/guardrails.md](containers/guardrails.md), [containers/opa.md](containers/opa.md)
