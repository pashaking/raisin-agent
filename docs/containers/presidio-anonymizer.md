# presidio-anonymizer

Microsoft Presidio's anonymizer: given text and analyzer results, replaces the spans. In the POC it has exactly one job,
on exactly one path: Donor-role output. Donors never get placeholders restored (they should not receive names from the
system of record), so instead of restoring, the runtime sends the draft and any `PERSON` hits here and gets back text with
`<REDACTED>` in their place.

| | |
|---|---|
| Image | `mcr.microsoft.com/presidio-anonymizer:latest` (resolved `2.2.362`) |
| Port | 15002 → 3000 |
| Memory limit | 512 MB |
| Health | `GET /health` (start period 30 s) |

## How the runtime calls it

`POST /anonymize {"text", "anonymizers": {"DEFAULT": {"type": "replace", "new_value": "<REDACTED>"}}, "analyzer_results": [{entity_type, start, end, score}, ...]}`
→ `{"text": "..."}`.

The call is made on every Donor response, even when `analyzer_results` is empty, so the anonymizer's availability is part
of the control: a Donor request cannot succeed while the anonymizer is down, regardless of whether anything needed masking.

## Spans

`guardrail.presidio.anonymize` (in the runtime) with `guardrail.service=presidio-anonymizer`, `guardrail.entities_masked`,
and `guardrail.verdict=unavailable` + exception on failure.

## When it is down

Donor requests: 400 `guardrail_unavailable`, `service=presidio-anonymizer`, at the output stage (the model has already
answered; the answer is discarded). Finance and Participant requests are unaffected. `make test-fail-closed` covers this.

## Success example

Scenario 2 and 9 (`donor.b@example.test`): draft "You have made two donations: ... Donation ID: 991203 ..." → analyzer finds
no PERSON → anonymizer called with an empty result list → returns the text unchanged → `guardrail.output verdict=pass`,
`entities_masked=0`. Trace `4d2c78178d59302008090825e2ab3aac` (scenario 9) contains the `guardrail.presidio.anonymize` span.

## Failure example

Stop the container and re-run scenario 9: HTTP 400 `{"error": "guardrail_unavailable", "service": "presidio-anonymizer", "verdict": "unavailable"}`.
Scenario 1 (Finance) in the same state: HTTP 200, unaffected. This asymmetry is the point.

## Related

- [presidio-analyzer.md](presidio-analyzer.md), [agent-runtime.md](agent-runtime.md) (`guardrails.output_stage`)
