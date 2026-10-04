# Donation AI Control Plane POC: documentation

Documentation for the runnable proof of concept in `infra-designs/poc/`. The design it implements is
`../../02_Donation_AI_Control_Plane_POC_Design.md`; the security review is `../../03_AI_Control_Plane_Security_Review.md`.
Everything below was verified against the running stack on 2026-09-07 (all example outputs are real responses and real
Jaeger traces, model `gpt-4o-mini`, temperature 0).

## Start here

| If you want to... | Read |
|---|---|
| See one request go through the whole control plane in three commands | [00-tutorial-first-trace.md](00-tutorial-first-trace.md) |
| Understand why the stack is shaped the way it is | [01-architecture.md](01-architecture.md) |
| Follow a request hop by hop (assistant, story, payment, tool call, guardrails) | [02-request-lifecycle.md](02-request-lifecycle.md) |
| See what a correct answer looks like at every layer, with the real trace | [03-success-paths.md](03-success-paths.md) |
| See every way a request can be stopped and what the caller, the model and the SIEM each see | [04-failure-paths.md](04-failure-paths.md) |
| Look up a span name or attribute, or the SIEM filter | [05-observability-reference.md](05-observability-reference.md) |
| Understand one container: purpose, config, endpoints, spans, what happens when it is down | [containers/README.md](containers/README.md) |
| Run, test, tune, inspect (commands) | [../RUNBOOK.md](../RUNBOOK.md) |
| Present the whole thing: design, architecture, infrastructure, request flow, failure map, observability, production-readiness gaps (slide-style page) | [06-poc-presentation.html](06-poc-presentation.html) |

## Diagrams

Source is the `.mmd` file in `infra-designs/diagrams/`; `.svg`, `.png` and `.excalidraw` are renders. Edit the `.mmd`, re-render with `/diagram`.

| # | Diagram | Used in |
|---|---|---|
| 07 | [Container topology](../../diagrams/07-poc-container-topology.png) | architecture, containers |
| 08 | [Assistant request sequence (scenario 1)](../../diagrams/08-poc-assistant-request-sequence.png) | request lifecycle, success paths |
| 09 | [Guardrail layers: input, retrieved, output](../../diagrams/09-poc-guardrail-layers.png) | request lifecycle, guardrails container |
| 10 | [Tool call authorization](../../diagrams/10-poc-tool-call-authorization.png) | request lifecycle, agent-runtime, opa |
| 11 | [Failure map: where a request stops and what the caller sees](../../diagrams/11-poc-failure-map.png) | failure paths |
| 12 | [Observability pipeline](../../diagrams/12-poc-observability-pipeline.png) | observability reference |
| 13 | [Story path with PII placeholders](../../diagrams/13-poc-story-pii-sequence.png) | request lifecycle, success paths |

Earlier design-level diagrams (01 placement, 02 lifecycle, 03 RAG/tools split, 04 multi-tenant RAG, 05 data minimization,
06 learning path) describe the target architecture; 07 to 13 describe what the POC actually runs.

## Conventions used in these pages

- **Success path**: HTTP 200, `guardrail.verdict=pass` on input and output, every `policy.decide` span `allow`.
- **Failure path**: any request the control plane stops or degrades. Some are hard stops (4xx/5xx to the caller), some are
  soft (200, but the model was told `not_found`, a chunk was flagged, or the answer was replaced by a refusal). Both kinds
  leave a red span in Jaeger and a row in the SIEM feed.
- **Fail closed**: when OPA, guardrails, NeMo or Presidio cannot be reached, the request is refused, never passed through.
- Shell examples run from `infra-designs/poc/`. Responses shown with `DEBUG_PANEL=on` (the console setting); with the
  default `off` the caller gets `answer` + `trace_id` only.
