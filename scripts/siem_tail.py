#!/usr/bin/env python3
"""Human-readable view of the SIEM feed (observability/siem/security-events.jsonl).

Each line the otel-collector writes is one OTLP batch that passed the security filter
(policy.result=deny, guardrail.verdict=block|flagged). Prints one row per span.

usage: python3 scripts/siem_tail.py          # dump the whole file
       python3 scripts/siem_tail.py -f       # follow (like tail -f)
"""
import json
import sys
import time

PATH = "observability/siem/security-events.jsonl"
KEYS = ("enduser.id", "tenant_id", "role", "policy.stage", "policy.result", "policy.reason", "policy.tool",
        "guardrail.verdict", "guardrail.service", "guardrail.flagged", "guardrail.risk_score")


def val(v):
    if "arrayValue" in v:
        return [val(x) for x in v["arrayValue"].get("values", [])]
    return next(iter(v.values()), None)


def rows(line):
    for rs in json.loads(line).get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                attrs = {a["key"]: val(a["value"]) for a in sp.get("attributes", [])}
                picked = {k: attrs[k] for k in KEYS if k in attrs}
                events = [e["name"] for e in sp.get("events", [])]
                yield f"{sp['traceId'][:16]}  {sp['name']:<40} {json.dumps(picked)}  events={events}"


def main():
    follow = "-f" in sys.argv
    with open(PATH) as f:
        while True:
            line = f.readline()
            if line:
                for r in rows(line):
                    print(r, flush=True)
            elif follow:
                time.sleep(0.5)
            else:
                break


if __name__ == "__main__":
    main()
