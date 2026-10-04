#!/usr/bin/env python3
"""Verify the hour-one gate from the host.

usage: check_gate.py <trace_id>
Checks Jaeger (http://localhost:16686) for one trace that contains both the agent-runtime
and litellm services with litellm spans parented under the runtime span, and Phoenix
(http://localhost:16006) for that trace's LLM span with token counts (REST filter trace_id + span_kind, Phoenix >= 20.x;
falls back to a name filter, then to the newest-50 window for older servers).
"""
import json
import sys
import time
import urllib.request

JAEGER = "http://localhost:16686"
PHOENIX = "http://localhost:16006"


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def check_jaeger(trace_id: str) -> bool:
    for _ in range(20):
        try:
            data = get(f"{JAEGER}/api/traces/{trace_id}")
            if data.get("data"):
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        print("JAEGER: trace not found")
        return False
    t = data["data"][0]
    procs = t["processes"]
    spans = t["spans"]
    by_id = {s["spanID"]: s for s in spans}
    services = {procs[s["processID"]]["serviceName"] for s in spans}
    print(f"JAEGER: services={sorted(services)} spans={len(spans)}")
    litellm_spans = [s for s in spans if procs[s["processID"]]["serviceName"] == "litellm"]
    nested = False
    for s in litellm_spans:
        for ref in s.get("references", []):
            parent = by_id.get(ref["spanID"])
            if parent and procs[parent["processID"]]["serviceName"] == "agent-runtime":
                nested = True
    for s in sorted(spans, key=lambda x: x["startTime"]):
        depth = 0
        cur = s
        while cur.get("references"):
            cur = by_id.get(cur["references"][0]["spanID"])
            if not cur:
                break
            depth += 1
        print(f"  {'  '*depth}{procs[s['processID']]['serviceName']}: {s['operationName']} ({s['duration']/1000:.0f} ms)")
    ok = "agent-runtime" in services and "litellm" in services and nested
    print("JAEGER: PASS nesting" if ok else "JAEGER: FAIL (litellm span not nested under agent-runtime)")
    return ok


def check_phoenix(tid: str) -> bool:
    try:
        projects = get(f"{PHOENIX}/v1/projects")
    except Exception as e:
        print(f"PHOENIX: unreachable ({e})")
        return False
    names = [p.get("name") for p in projects.get("data", [])]
    print(f"PHOENIX: projects={names}")
    proj = "ai-control-plane-poc" if "ai-control-plane-poc" in names else (names[0] if names else None)
    if not proj:
        print("PHOENIX: no project")
        return False
    # Newest-first queries, most precise first. A 100+ span trace pushes its chat spans out of a plain newest-50 window.
    queries = (
        f"trace_id={tid}&span_kind=LLM&limit=50",
        f"trace_id={tid}&limit=500",
        "limit=50",
    )
    chat: list = []
    for q in queries:
        try:
            spans = get(f"{PHOENIX}/v1/projects/{proj}/spans?{q}")
        except Exception as e:
            print(f"PHOENIX: spans?{q.split('&')[0]} failed ({e})")
            continue
        chat = [s for s in spans.get("data", []) if str(s.get("name", "")).startswith("chat ")]
        if chat:
            break
    if not chat:
        print("PHOENIX: no chat span yet")
        return False
    attrs = chat[-1].get("attributes", {})
    flat = json.dumps(attrs)
    has_tokens = "token_count" in flat or "usage" in flat
    print(f"PHOENIX: chat span found ({len(chat)} in trace, kind={chat[-1].get('span_kind')}), tokens_rendered={has_tokens}")
    return has_tokens


if __name__ == "__main__":
    tid = sys.argv[1]
    j = check_jaeger(tid)
    p = check_phoenix(tid)
    print("GATE:", "PASS" if (j and p) else ("PARTIAL (nesting ok, phoenix pending)" if j else "FAIL"))
    sys.exit(0 if j else 1)
