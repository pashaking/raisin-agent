# presidio-analyzer

Microsoft Presidio's analyzer: finds PII spans in text and scores them. In the POC it runs twice per request, on the
user's input (to create placeholders before the model sees anything) and on the model's output (to block anything that
looks like an email, phone number or card number that was not one of this request's placeholders). Called by the
[agent-runtime](agent-runtime.md), not by the guardrails service.

| | |
|---|---|
| Image | `mcr.microsoft.com/presidio-analyzer:latest` (resolved to `2.2.362` here; F13 says pin) |
| Port | 15001 → 3000 |
| Memory limit | 1.5 GB (spaCy model) |
| Health | `GET /health` (start period 60 s) |

## How the runtime calls it

`POST /analyze {"text", "language": "en", "entities": ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD"], "score_threshold": 0.3}`
→ list of `{entity_type, start, end, score}`.

The service is queried at 0.3 and the runtime applies per-entity thresholds: `PERSON 0.6`, `EMAIL_ADDRESS 0.6`,
`PHONE_NUMBER 0.4`, `CREDIT_CARD 0.6`. The phone threshold is low because Presidio's phone recognizer scores 0.4 without a
context word ("call", "phone") and 0.75 with one. Transaction ids, dates and charity names were verified (2026-09-06) not to
score as any of these four types.

Input stage: results are sorted by `start` descending and replaced with `<TYPE_n>` tokens; identical strings share one
token; the mapping stays in the runtime's memory for the life of the request.

Output stage: `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD` in the draft → block (refusal text, `reason=pii_in_output`).
`PERSON` → `flagged` only (names appear legitimately in stories and are restored from placeholders).

## Quirks that shaped the fixtures

- Presidio validates email TLDs. `.test` addresses are never detected, so the identity emails (`finance.a@charity-a.test`)
  and the PII canary (`canary.donor@pii-canary.test`) stay on `.test` on purpose (they must never be placeholdered or
  appear in a span), while donors that demos look up by email use `example.com`. A `.test` email typed into the console
  reaches the NeMo judge raw and is blocked as a PII lookup.
- Phone numbers without context words score 0.4, hence the threshold.

## Spans

`guardrail.presidio.analyze` (in the runtime) with `guardrail.service=presidio-analyzer`, `guardrail.entities` as
`["TYPE:count", ...]`, and `guardrail.verdict=unavailable` + exception on failure. The runtime's span, not the container's:
the analyzer image is not instrumented.

## When it is down

`GuardrailUnavailable("presidio-analyzer")` at the input stage → 400 `guardrail_unavailable`, `service=presidio-analyzer`.
Nothing reaches the model.

## Success example

Scenario 5 input:

```bash
curl -s localhost:15001/analyze -H 'Content-Type: application/json' \
  -d '{"text":"My name is Jane Doe, call 604-555-0142 or email jane.doe@example.com","language":"en","entities":["PERSON","EMAIL_ADDRESS","PHONE_NUMBER","CREDIT_CARD"],"score_threshold":0.3}'
```

```json
[{"entity_type": "EMAIL_ADDRESS", "start": 48, "end": 68, "score": 1.0},
 {"entity_type": "PERSON",        "start": 11, "end": 19, "score": 0.85},
 {"entity_type": "PHONE_NUMBER",  "start": 26, "end": 38, "score": 0.4}]
```

All three pass the per-entity thresholds. The runtime produced `<PERSON_1>`, `<PHONE_NUMBER_1>`, `<EMAIL_ADDRESS_1>`
(span `guardrail.entities=["EMAIL_ADDRESS:1","PERSON:1","PHONE_NUMBER:1"]`, `guardrail.placeholders=3`), and the response's
`model_saw.placeholders` listed the same three tokens.

## Failure examples

Fail closed (F10), analyzer stopped, trace `c595b2b29baa4793a254071f6fdbffaf`:

```
HTTP 400 {"detail": {"error": "guardrail_unavailable", "service": "presidio-analyzer", "verdict": "unavailable", "trace_id": "c595b2b2..."}}
guardrail.input ERROR
 guardrail.presidio.analyze  verdict=unavailable  events [exception, exception]
```

Output block by design: the first `find_donor` implementation returned `a***@example.com`; the output-stage analyze found an
`EMAIL_ADDRESS` that was not a placeholder → `presidio_block=1`, refusal text. The tool now returns donor id and totals only.

## Related

- [presidio-anonymizer.md](presidio-anonymizer.md) (Donor output masking), [agent-runtime.md](agent-runtime.md) (`guardrails.py` thresholds and stages)
- Memory note on TLD / phone behaviour: `services/seed/fixtures.py` comments
