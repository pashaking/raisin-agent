# seed and gate (one-shot jobs)

Two compose services behind profiles, run with `docker compose run --rm`, never long-lived.

## seed (`profile: seed`)

Loads everything the running stack needs but does not create for itself: schema and fixtures, LiteLLM virtual keys, the
AI registry document in OPA, and the embedded knowledge base. Each unit is its own root span so seed traces never mix with
request traces.

| | |
|---|---|
| Build | `services/seed/Dockerfile` (psycopg, httpx) |
| Code | `services/seed/seed.py`, `fixtures.py`, `schema.sql`, `kb_schema.sql`, `docs/` (10 markdown files) |
| Env | `RAISIN_DATABASE_URL`, `KB_DATABASE_URL`, `LITELLM_BASE_URL`, `LITELLM_MASTER_KEY`, the five `LITELLM_KEY_*` values, `OPA_URL`, `OPA_TOKEN_SEED`, `REGISTRY_PATH=/policy-data/registry.json` (bind mount `./policy/data`, read-write) |
| Depends on | `postgres` healthy, `litellm` healthy, `opa` started |

### Commands

| `python seed.py ...` | Make target | Does |
|---|---|---|
| `all` (default) | `make seed` | `schema` → `keys` → `registry` → `index` (index failure is non-fatal: re-run `index` once `OPENAI_API_KEY` works) |
| `schema` | | applies `schema.sql` to `raisin` and `kb_schema.sql` to `kb`; upserts tenants, users, donors, donations, transactions, `ai_registry` rows from `fixtures.py` |
| `keys` | | `GET /key/list` with the master key, then `POST /key/generate` for each missing alias with `max_budget`, `budget_duration`, `models`, `metadata.tenant` |
| `index` | | embeds each doc as one chunk via `POST litellm/embeddings` (`text-embedding-3-small`, `seed-indexer` key) and upserts into `kb.chunks` with tenant and classification from `fixtures.DOCS` |
| `registry` | | reads `ai_registry`, writes `policy/data/registry.json` with `revision = sha256(document)[:16]`, `PUT /v1/data/registry` to OPA with the seed token |
| `flip <tool> <true\|false>` | `make registry-flip TOOL=.. APPROVED=..` | `UPDATE ai_registry SET approved`, then `registry` |
| `reset` | `make registry-reset` | restores fixture approvals, then `registry` |

### Fixtures worth knowing

- Personas: `finance.a@charity-a.test` (Finance, tenant-a), `finance.b@charity-b.test` (Finance, tenant-b),
  `donor.b@example.test` (Donor, tenant-b), `participant.a@example.test` (Participant, tenant-a),
  `admin@aka.com` (Finance, tenant-c), `donor@pmcf.ca` (Donor, tenant-c).
- Donations: 873928 (A, declined 51, canary donor), 873901..873904 (A), 991201..991204 (B), 12345 + 774101..774111 (C;
  three declines with codes 51/05/14; 774107..774111 are one recurring donor, all approved). 991204 is the id the poisoned document tries to make the assistant fetch.
- PII canary: `canary.donor@pii-canary.test`, `604-555-0199`. Must appear in zero spans (`make test`).
- Knowledge docs: 4 platform docs, 2 tenant-a (+ 1 poisoned), 2 tenant-b, 1 `internal`. One chunk per doc on purpose so
  the poisoned procedure and its injection paragraph rank together.
- Registry: `legacy_summarizer` unapproved (OPA test case); `resend_receipt` approved, in Finance's role list, and
  tagged `action_risk=high` so the tool stage always denies it with `requires_approval` (Phase 10's approval queue
  is the only path that can run it) — see [opa.md](opa.md#risk-classification). Every other tool is `low` except
  `get_donor_profile` / `find_donor` (`medium`, identify a donor) and `save_task_state` (`medium`, a write).
- Memory: `MEMORY_PREFS` seeds one `kind='preference'` row in `kb.agent_memory` for `finance.a@charity-a.test`
  (`digest_frequency: daily`), read back through `get_user_context` — see
  [agent-runtime.md](agent-runtime.md#agent-memory-memorypy-phase-8).
- Approvals: no fixtures -- `kb.approval_requests` starts empty; rows are created only when a model actually calls
  `resend_receipt` (or, for deterministic testing, `scripts/test_approvals.py` seeds rows directly inside the
  running container) — see [agent-runtime.md](agent-runtime.md#human-approval-workflow-approvalspy-phase-10).

### Spans

`seed.schema`, `seed.keys` (`keys.created`), `seed.index` (`kb.docs`), `seed.registry` (`policy.registry_revision`).

### Success example

```
$ make seed
seed.schema ok
seed.keys ok created=[]          # keys already existed from a previous run
seed.registry ok revision=b6d21bfcf80e4165 file=/policy-data/registry.json
seed.index ok docs=10
seed done in 6.3s
```

`make opa-data PATH=registry/revision` then returns `"b6d21bfcf80e4165"`, and the next `policy.decide` span carries it.

### Failure example

`seed.index FAILED (non-fatal): AuthenticationError: ...` when `OPENAI_API_KEY` is missing or invalid behind LiteLLM.
Everything else is seeded; `search_kb` returns zero chunks until `docker compose run --rm seed python seed.py index` succeeds.

## gate (`profile: gate`)

The hour-one risk gate from the design: before building anything else, prove that a hand-rolled `chat <model>` span with
`gen_ai.*` attributes, calling LiteLLM through the OpenAI SDK, ends up with LiteLLM's `litellm_request` span nested under
it (traceparent propagation works) and with token counts visible in Phoenix.

| | |
|---|---|
| Build | same image as `agent-runtime`, command `python gate.py` |
| Env | `LITELLM_BASE_URL`, `LITELLM_KEY=LITELLM_MASTER_KEY`, `GATE_MODEL` (default `gpt-4o-mini`) |
| Run | `make up-gate` (only postgres, jaeger, phoenix, otel-collector, litellm), `make gate` → prints `TRACE_ID=<hex>`, `make check-gate TRACE_ID=<hex>` |

`scripts/check_gate.py` queries Jaeger for that trace and asserts that `agent-runtime` and `litellm` are both present and
that the LiteLLM spans are parented under the runtime span, then checks Phoenix for the chat span. Prints `GATE: PASS`,
`PARTIAL (nesting ok, phoenix pending)` or `FAIL`. Status in the README: PASS.

## Related

- [postgres.md](postgres.md), [litellm.md](litellm.md), [opa.md](opa.md)
- Registry flip scenario: [../03-success-paths.md](../03-success-paths.md#s6-registry-flip-tool-disappears-without-a-restart)
