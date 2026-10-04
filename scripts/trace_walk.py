#!/usr/bin/env python3
"""Print one trace as an indented step-by-step tree with the content-bearing attributes of every hop.

usage: trace_walk.py <trace_id> [--full] [--noise]
  --full   do not truncate attribute values (prompts, messages, tool args)
  --noise  keep the `http receive` / `http send` ASGI spans
Reads the Jaeger API (JAEGER_URL, default http://localhost:16686). What each attribute means and which ones are gated by
DEBUG_TRACE_CONTENT: docs/05-observability-reference.md, "Following one request step by step".
"""
import json
import os
import sys
import urllib.request

JAEGER = os.environ.get("JAEGER_URL", "http://localhost:16686")
# attributes worth reading per hop, in display order
CONTENT = (
    "gen_ai.prompt.name", "enduser.id", "tenant_id", "role",
    "policy.stage", "policy.input.prompt", "policy.input.model", "policy.input.tool", "policy.input.resource_tenant",
    "policy.result", "policy.reason", "policy.decision_id", "policy.tools.allowed", "policy.tools.withheld",
    "guardrail.verdict", "guardrail.entities", "guardrail.entities_masked", "guardrail.placeholders",
    "guardrail.nemo.status", "guardrail.nemo.rail", "guardrail.risk_score", "guardrail.reasons",
    "guardrail.moderation.categories", "guardrail.model",
    "input.value", "output.value", "gen_ai.tool.calls", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.input.messages", "gen_ai.output.messages", "llm.openai.messages", "metadata.user_api_key_alias",
    "tool.parameters", "tool.result", "tool.deny_reason", "tool.result.fields", "tool.arg.resolved_placeholder",
    "retrieval.filter.tenants", "retrieval.documents", "donation.id", "transaction.id",
    "authz.tool", "authz.result", "db.sql.table", "db.rows", "payment.result", "payment.decline_code", "http.status_code",
)
NOISE = ("http receive", "http send")
LIMIT = 220


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    full, noise = "--full" in sys.argv, "--noise" in sys.argv
    with urllib.request.urlopen(f"{JAEGER}/api/traces/{args[0]}", timeout=30) as r:
        data = json.load(r)["data"]
    if not data:
        print("trace not found in Jaeger:", args[0])
        sys.exit(1)
    t = data[0]
    procs = {k: v["serviceName"] for k, v in t["processes"].items()}
    spans = sorted(t["spans"], key=lambda s: s["startTime"])
    byid = {s["spanID"]: s for s in spans}

    def depth(s: dict) -> int:
        d = 0
        while True:
            parents = [ref for ref in s.get("references", []) if ref["refType"] == "CHILD_OF"]
            if not parents or parents[0]["spanID"] not in byid:
                return d
            s = byid[parents[0]["spanID"]]
            d += 1

    print(f"trace {t['traceID']}  spans {len(spans)}  {JAEGER}/trace/{t['traceID']}")
    for s in spans:
        if not noise and any(n in s["operationName"] for n in NOISE):
            continue
        ind = "  " * depth(s)
        tags = {x["key"]: x["value"] for x in s["tags"]}
        status = "  ERROR" if tags.get("otel.status_code") == "ERROR" or tags.get("error") else ""
        print(f"{ind}[{procs[s['processID']]}] {s['operationName']}  ({s['duration'] / 1000:.0f}ms){status}")
        for k in CONTENT:
            if k in tags:
                v = str(tags[k]).replace("\n", "\\n")
                if not full and len(v) > LIMIT:
                    v = f"{v[:LIMIT]} …({len(v)} chars)"
                print(f"{ind}    {k} = {v}")
        for lg in s.get("logs", []):
            f = {x["key"]: x["value"] for x in lg["fields"]}
            ev = f.get("event")
            if not ev:
                continue
            detail = {k: v for k, v in f.items() if k != "event"}
            if ev == "exception":
                detail = {k: str(v)[:LIMIT] for k, v in detail.items() if k in ("exception.type", "exception.message")}
            print(f"{ind}    EVENT {ev} {json.dumps(detail, default=str)}")


if __name__ == "__main__":
    main()
