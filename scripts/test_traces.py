#!/usr/bin/env python3
"""Trace assertions against Jaeger. Run after `make demo`.

  1. PCI scope (non-vacuous): among traces rooted at raisin-api, >=1 touches payment-gateway, >=1 has a
     gen_ai.* span, and none has both.
  2. PII canary: no span attribute/event anywhere contains the canary email or phone.
  2b. Trace content minimized: agent-runtime input.value / output.value / tool.parameters carry sha256 + structure only
      (skipped when the runtime reports DEBUG_TRACE_CONTENT=on).
  2c. Gateway spans carry no message content (F14): no span of any service carries gen_ai.input.messages /
      gen_ai.output.messages / gen_ai.prompt.N.* / gen_ai.completion.N.* / llm.input_messages.* / llm.output_messages.* /
      llm.<provider>.(messages|choices|input|data) (raw request and response echo, embedding inputs). Never skipped: the runtime's gated input.value is the only content channel. Only traces that
      started after the running litellm container did are judged, so a config fix is testable at once.
  3. --fail-closed: stop opa / guardrails / nemo-guardrails / presidio-analyzer / presidio-anonymizer one at a time, run a
     scenario, expect the runtime to refuse with unavailable, restart and wait for healthy.
"""
import json
import re
import subprocess
import sys
import time
import urllib.request

JAEGER = "http://localhost:16686"
CANARY = ["canary.donor@pii-canary.test", "604-555-0199"]
SERVICES = ["raisin-api", "agent-runtime", "litellm", "opa", "payment-gateway", "seed"]


def get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def traces_for(service: str):
    return get(f"{JAEGER}/api/traces?service={service}&lookback=6h&limit=500").get("data", [])


def span_service(t, s):
    return t["processes"][s["processID"]]["serviceName"]


def root_service(t):
    roots = [s for s in t["spans"] if not s.get("references")]
    return span_service(t, roots[0]) if roots else None


def has_gen_ai(t):
    return any(k["key"].startswith("gen_ai.") for s in t["spans"] for k in s.get("tags", []))


def touches(t, service):
    return any(span_service(t, s) == service for s in t["spans"])


def test_pci_scope() -> bool:
    ts = [t for t in traces_for("raisin-api") if root_service(t) == "raisin-api"]
    pay = [t for t in ts if touches(t, "payment-gateway")]
    ai = [t for t in ts if has_gen_ai(t)]
    both = [t for t in ts if touches(t, "payment-gateway") and has_gen_ai(t)]
    print(f"PCI: raisin-api-rooted traces={len(ts)} payment={len(pay)} ai={len(ai)} both={len(both)}")
    ok = len(pay) >= 1 and len(ai) >= 1 and len(both) == 0
    print("PCI scope:", "PASS" if ok else "FAIL")
    return ok


def test_pii_canary() -> bool:
    hits = []
    seen = set()
    for svc in SERVICES:
        for t in traces_for(svc):
            if t["traceID"] in seen:
                continue
            seen.add(t["traceID"])
            blob = json.dumps(t)
            for c in CANARY:
                if c in blob:
                    hits.append((t["traceID"], c))
    print(f"PII canary: traces scanned={len(seen)} hits={len(hits)}")
    for h in hits[:10]:
        print("  HIT", h)
    print("PII canary:", "PASS" if not hits else "FAIL")
    return not hits


def _runtime_flags() -> dict:
    try:
        return get("http://localhost:18090/health")
    except Exception:  # noqa: BLE001
        return {}


def test_trace_content_minimized() -> bool:
    """F1: LLM spans must not export message bodies (user text, tenant KB chunks, tool results). input.value / output.value
    and tool.parameters carry structure + sha256 only, unless the runtime runs with DEBUG_TRACE_CONTENT=on (local demo)."""
    flags = _runtime_flags()
    if flags.get("trace_content") == "on":
        print("trace content: SKIP (runtime DEBUG_TRACE_CONTENT=on)")
        return True
    bad = []
    seen = 0
    for t in traces_for("agent-runtime"):
        for s in t["spans"]:
            if span_service(t, s) != "agent-runtime":
                continue
            tags = {k["key"]: str(k["value"]) for k in s.get("tags", [])}
            for key in ("input.value", "output.value", "tool.parameters"):
                v = tags.get(key)
                if v is None:
                    continue
                seen += 1
                try:
                    d = json.loads(v)
                except ValueError:
                    d = None
                ok = isinstance(d, dict) and "sha256" in d and not any(isinstance(x, list) and x and isinstance(x[0], dict) and "content" in x[0] for x in d.values())
                if not ok:
                    bad.append((t["traceID"], s["operationName"], key, v[:80]))
    print(f"trace content: attributes checked={seen} leaking={len(bad)}")
    for b in bad[:10]:
        print("  LEAK", b)
    print("trace content:", "PASS" if not bad else "FAIL")
    return not bad


GATEWAY_CONTENT_KEYS = ("gen_ai.input.messages", "gen_ai.output.messages", "llm.openai.messages")
# numbered legacy forms (gen_ai.prompt.0.content, gen_ai.completion.0.function_call.arguments), OpenInference message
# lists, and LiteLLM's raw provider echo on raw_gen_ai_request (llm.openai.messages / .choices, llm.openrouter.*,
# llm.None.input = embedding input text, llm.None.data = vectors). The runtime's own gen_ai.prompt.name (template name)
# and llm.token_count.* / llm.model_name / llm.invocation_parameters are not matched.
GATEWAY_CONTENT_RE = re.compile(r"^(gen_ai\.(prompt|completion)\.\d+\.|llm\.(input|output)_messages|llm\.[^.]+\.(messages|choices|input|data)$)")


def _container_started_us(service: str) -> int:
    """Start time (epoch microseconds) of the running compose container, 0 if unknown. Boundary for checks that judge
    the current configuration of that service, not spans it exported under an earlier one."""
    try:
        cid = subprocess.run(["docker", "compose", "ps", "-q", service], capture_output=True, text=True, timeout=20).stdout.strip()
        started = subprocess.run(["docker", "inspect", "--format", "{{.State.StartedAt}}", cid], capture_output=True, text=True, timeout=20).stdout.strip()
        from datetime import datetime, timezone

        started = started[:26].rstrip("Z")  # 2026-09-12T17:00:00.123456789Z -> microsecond precision
        return int(datetime.fromisoformat(started).replace(tzinfo=timezone.utc).timestamp() * 1_000_000)
    except Exception:  # noqa: BLE001
        return 0


def _is_gateway_content_key(key: str) -> bool:
    return key in GATEWAY_CONTENT_KEYS or bool(GATEWAY_CONTENT_RE.match(key))


def test_gateway_spans_carry_no_message_content() -> bool:
    """F14: LiteLLM's otel callback exported full prompts and completions on litellm_request / raw_gen_ai_request
    regardless of DEBUG_TRACE_CONTENT. No span of any service may carry those attribute names."""
    since = _container_started_us("litellm")
    bad = []
    seen = set()
    for svc in SERVICES:
        for t in traces_for(svc):
            if t["traceID"] in seen:
                continue
            seen.add(t["traceID"])
            if min(s["startTime"] for s in t["spans"]) < since:
                continue
            for s in t["spans"]:
                for k in s.get("tags", []):
                    if _is_gateway_content_key(k["key"]):
                        bad.append((t["traceID"], span_service(t, s), s["operationName"], k["key"], str(k["value"])[:60]))
    print(f"gateway content: traces scanned={len(seen)} since litellm start={'yes' if since else 'no (docker unavailable)'} leaking attributes={len(bad)}")
    for b in bad[:10]:
        print("  LEAK", b)
    print("gateway content:", "PASS" if not bad else "FAIL")
    return not bad


def _compose(*args):
    return subprocess.run(["docker", "compose", *args], capture_output=True, text=True)


def _wait_healthy(service: str, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        out = _compose("ps", "--format", "json", service).stdout
        try:
            rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        except json.JSONDecodeError:
            rows = []
        if rows and ("healthy" in (rows[0].get("Health") or "") or (rows[0].get("Health") in ("", None) and rows[0].get("State") == "running")):
            if service == "opa":  # no docker healthcheck and no host port (token-authenticated); probe from inside the network
                r = _compose("exec", "-T", "agent-runtime", "python", "-c", "import urllib.request;urllib.request.urlopen('http://opa:8181/health',timeout=3)")
                if r.returncode != 0:
                    time.sleep(2)
                    continue
            return True
        time.sleep(5)
    return False


def _scenario(user: str, question: str):
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    import demo  # noqa: WPS433

    st, b, _ = demo.call("POST", "/assistant", {"question": question}, demo.token(user))
    return st, b


def test_fail_closed() -> bool:
    ok = True
    cases = [
        ("opa", "finance.a@charity-a.test", 403, "policy_deny", "unavailable"),
        ("guardrails", "finance.a@charity-a.test", 400, "guardrail_unavailable", "guardrails"),
        ("nemo-guardrails", "finance.a@charity-a.test", 400, "guardrail_unavailable", "guardrails"),
        ("presidio-analyzer", "finance.a@charity-a.test", 400, "guardrail_unavailable", "presidio-analyzer"),
        ("presidio-anonymizer", "donor.b@example.test", 400, "guardrail_unavailable", "presidio-anonymizer"),
    ]
    for svc, user, want_status, want_error, want_marker in cases:
        _compose("stop", svc)
        time.sleep(2)
        st, b = _scenario(user, "Why was donation 873928 declined?")
        d = b.get("detail", {}) if isinstance(b, dict) else {}
        got = f"{st} {d.get('error')} {d.get('reason') or d.get('service')}"
        passed = st == want_status and d.get("error") == want_error and want_marker in got
        print(f"fail-closed {svc}: got [{got}] want [{want_status} {want_error} {want_marker}] ->", "PASS" if passed else "FAIL")
        ok &= passed
        _compose("start", svc)
        if not _wait_healthy(svc):
            print(f"  WARNING: {svc} did not report healthy after restart")
            ok = False
    return ok


if __name__ == "__main__":
    results = [test_pci_scope(), test_pii_canary(), test_trace_content_minimized(), test_gateway_spans_carry_no_message_content()]
    if "--fail-closed" in sys.argv:
        results.append(test_fail_closed())
    print("\nTRACE TESTS:", "PASS" if all(results) else "FAIL")
    sys.exit(0 if all(results) else 1)
