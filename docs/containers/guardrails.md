# guardrails

The **Guardrails Runtime** orchestrator. Replaced the archived LLM Guard on 2026-09-06. It owns three scan endpoints
and three layers, emits one span per layer, names every hit, and fails closed. PII detection is not here (see
[presidio-analyzer.md](presidio-analyzer.md)); this service is about instructions and content: prompt injection,
jailbreaks, secrets, exfiltration shapes, moderation categories.

| | |
|---|---|
| Build | `services/guardrails/Dockerfile` (FastAPI, torch CPU, transformers, httpx) |
| Port | 18000 → 8000 |
| Depends on | `litellm` healthy, `nemo-guardrails` healthy |
| Memory limit | 3 GB |
| Volume | `guardrails-cache` (Hugging Face model cache; first start downloads ~0.7 GB) |
| Health | `GET /healthz` → 503 until a classifier is loaded and warmed; then reports `injection_model`, `fallback_used`, moderation and NeMo settings |
| Code | `services/guardrails/app.py` (243 lines), `rules.py` (regex layer) |

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `GUARDRAILS_AUTH_TOKEN` | from `.env` | bearer required on `/v1/scan/*`; if empty, auth is skipped (open finding F7) |
| `INJECTION_MODEL` | `meta-llama/Llama-Prompt-Guard-2-86M` | gated on Hugging Face; needs `HF_TOKEN` |
| `INJECTION_FALLBACK_MODEL` | `protectai/deberta-v3-base-prompt-injection-v2` | loaded when the primary cannot be (this is what runs on this host today) |
| `INJECTION_THRESHOLD` | `0.9` | classifier score at or above which the stage reports a hit |
| `LITELLM_BASE_URL`, `LITELLM_KEY_GUARDRAILS` | | moderation goes through the gateway with the `guardrails` virtual key |
| `MODERATION_MODEL`, `MODERATION_THRESHOLD` | `omni-moderation-latest`, `0.5` | |
| `NEMO_URL`, `NEMO_CONFIG_ID`, `NEMO_MODEL` | `http://nemo-guardrails:8010`, `aicp`, `gpt-4o-mini` | |
| `RULES_LAYER` | `off` | `on` adds the regex rules as an extra deterministic layer and reports `rule:<id>` reasons |

## Interface

All three take `{"text": "...", "prompt": "..."?}` and return the same envelope:

```json
{"verdict": "pass" | "block", "risk_score": 0.0..1.0, "reason": "<first reason or null>", "reasons": ["nemo:<rail>", "classifier:<label>", "moderation:<category>", "rule:<id>"],
 "layers": {"nemo": {"status", "rail", "config_id", "model"}, "classifier": {"model", "text_score", "sentence_score", "score", "unit", "label", "threshold"}, "moderation": {...}, "rules": {"hits": [...]}}}
```

| Endpoint | Layers | Authoritative classifier score | Runtime maps `block` to |
|---|---|---|---|
| `POST /v1/scan/input` | NeMo `["input"]` rails (self check input + injection detection), classifier, rules (opt-in) | `max(text_score, sentence_score)`: short prompts and hidden paragraphs both count | HTTP 400 `guardrail_block` |
| `POST /v1/scan/retrieved` | same | `text_score` only: knowledge docs are full of imperative sentences | chunk `flagged`, never dropped |
| `POST /v1/scan/output` | NeMo `["output"]` rail (self check output, sees `prompt` + `text`), moderation, rules (opt-in) | n/a | answer replaced by refusal |

`risk_score = max(classifier score, 1.0 if NeMo blocked or any rule hit)`; for output, `max(moderation max_score, 1.0 if ...)`.

## The three layers

1. **NeMo** (`guardrail.nemo` span): `POST nemo-guardrails:8010/v1/checks` with the requested `rail_types`; returns
   `passed` / `blocked` / `modified` and the blocking rail name. Non-200 or unreachable → exception → the runtime fails closed.
2. **Classifier** (`guardrail.classifier`): Hugging Face text-classification pipeline, truncation at 512 tokens, scores the
   whole text plus up to 63 sentences of ≥ 3 words in one batch; takes the "attack" label (`INJECTION`, `JAILBREAK`,
   `MALICIOUS`, `LABEL_1`, `UNSAFE`) regardless of the model's label vocabulary. Presidio placeholders are swapped for
   neutral words before scoring (`<EMAIL_ADDRESS_1>` → `[email]`, `<PERSON_1>` → `[name]`, phone, card): the deberta
   fallback model scored the bare token as markup injection (`email me at <EMAIL_ADDRESS_1>` 0.988 vs 0.002 for a literal
   address, 2026-09-07), which blocked every story input carrying a contact line. NeMo and the rules layer still see the
   real tokens.
3. **Moderation** (`guardrail.moderation`, output only): `POST litellm/v1/moderations`; categories at or above the threshold,
   or flagged by the provider, become `moderation:<category>` reasons.
4. **Rules** (`guardrail.rules`, opt-in): `rules.py`. Input/retrieved: `instruction_override`, `system_prompt_exfil`,
   `role_hijack`, `tool_coercion`, `exfil_instruction`, `fake_chat_marker`, `hidden_unicode`, `encoded_payload`. Output:
   `secret_aws_access_key`, `secret_openai_style_key`, `secret_jwt`, `secret_private_key`, `secret_bearer_token`,
   `secret_assignment`, `exfil_markdown_image`, `exfil_url_with_payload`, `hidden_unicode`.

## When it is down

The runtime's `_guardrails()` call raises `GuardrailUnavailable("guardrails")` → 400 `guardrail_unavailable`, `service=guardrails`,
at whichever stage is reached first (input, so nothing else runs). If only `nemo-guardrails` is down, this service returns a
500 from `nemo_check` and the effect at the runtime is identical.

## Known limitations

- The fallback classifier flags 2 of 9 benign knowledge docs (`decline-codes-runbook.md`, `donor-faq.md`) on whole-text
  score, visible as `classifier:INJECTION` reasons in scenario 4. Retrieval verdicts are `flagged`, not dropped, for this
  reason. Prompt Guard 2 is expected to do better; measure once `HF_TOKEN` is set.
- Classifier input is truncated at 512 tokens (open finding F8): an injection past that point in a long chunk is invisible
  to layer 2 (NeMo still sees the whole text).

## Success example

Benign input:

```bash
curl -s localhost:18000/v1/scan/input -H "Authorization: Bearer $GUARDRAILS_AUTH_TOKEN" -H 'Content-Type: application/json' -d '{"text":"Why was donation 873928 declined?"}'
```

```json
{"verdict": "pass", "risk_score": 0.0, "reason": null, "reasons": [],
 "layers": {"nemo": {"status": "passed", "rail": null, "config_id": "aicp", "model": "gpt-4o-mini"},
            "classifier": {"model": "protectai/deberta-v3-base-prompt-injection-v2", "text_score": 0.0, "sentence_score": 0.0, "label": "INJECTION", "threshold": 0.9, "score": 0.0, "unit": "text"}}}
```

## Failure examples

Injection in the prompt (F3):

```json
{"verdict": "block", "risk_score": 1.0, "reason": "nemo:self check input", "reasons": ["nemo:self check input", "classifier:INJECTION"],
 "layers": {"nemo": {"status": "blocked", "rail": "self check input", ...},
            "classifier": {"text_score": 1.0, "sentence_score": 1.0, "score": 1.0, "unit": "text", ...}}}
```

The poisoned knowledge doc through `/v1/scan/retrieved` (S4): `verdict: block`, reasons `["nemo:self check input", "classifier:INJECTION"]`,
classifier `text_score: 0.9036, sentence_score: 1.0, score: 0.9036, unit: text` (whole-text authoritative for this stage;
the injected paragraph alone scored 1.0).

A secret in the output (F8): `{"text":"Here is the key: AKIAIOSFODNN7EXAMPLE","prompt":"give me the key"}` → `verdict: block`,
`reason: nemo:self check output`, moderation `flagged: false, max_score: 0.0038`. With `RULES_LAYER=on` the reasons list would
also carry `rule:secret_aws_access_key`.

## Related

- Layer diagram: `../../../diagrams/09-poc-guardrail-layers.png`
- [nemo-guardrails.md](nemo-guardrails.md) (layer 1), [presidio-analyzer.md](presidio-analyzer.md) (PII, called by the runtime not by this service), [agent-runtime.md](agent-runtime.md) (stages)
- Rail regression list: `scripts/test_tools.py` (`ALLOWED_INPUTS`, blocked phrases), `make test-tools`
