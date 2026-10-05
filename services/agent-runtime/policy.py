"""Policy Enforcement Point. Every model selection and every tool call asks OPA. Fails closed."""
import httpx
from opentelemetry import trace

import config

tracer = trace.get_tracer("agent-runtime.policy")


def decide(stage: str, claims: dict, **extra) -> dict:
    """Returns OPA's decision object plus decision_id. On timeout / error returns a synthetic deny with
    reason=unavailable (fail closed) and marks the span."""
    inp = {"stage": stage, "subject": {"sub": claims["sub"], "tenant_id": claims["tenant_id"], "role": claims["role"]}, **extra}
    with tracer.start_as_current_span("policy.decide") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("policy.engine", "opa")
        span.set_attribute("policy.stage", stage)
        for k in ("model", "tool", "prompt"):
            if k in extra and extra[k]:
                span.set_attribute(f"policy.input.{k}", extra[k])
        if "resource" in extra:
            span.set_attribute("policy.input.resource_tenant", extra["resource"].get("tenant_id", ""))
        try:
            with httpx.Client(timeout=config.POLICY_TIMEOUT_S, headers={"Authorization": f"Bearer {config.OPA_TOKEN}"}) as c:
                r = c.post(f"{config.OPA_URL}/v1/data/aicp/authz/decision", json={"input": inp})
            r.raise_for_status()
            body = r.json()
            d = body.get("result") or {"allow": False, "reason": "no_result"}
            d["decision_id"] = body.get("decision_id", "")
        except Exception as e:  # noqa: BLE001
            d = {"allow": False, "reason": "unavailable", "stage": stage, "decision_id": "", "error": type(e).__name__}
            span.record_exception(e)
        result = "allow" if d.get("allow") else "deny"
        span.set_attribute("policy.result", result)
        span.set_attribute("policy.reason", d.get("reason", ""))
        span.set_attribute("policy.decision_id", d.get("decision_id", ""))
        if d.get("registry_revision"):
            span.set_attribute("policy.registry_revision", d["registry_revision"])
        if stage == "model" and d.get("allow"):
            span.set_attribute("policy.tools.allowed", d.get("allowed_tools", []))
            span.set_attribute("policy.tools.withheld", d.get("withheld_tools", []))
            span.set_attribute("policy.model", d.get("model", ""))
        if stage in ("tool", "approval_execute") and d.get("risk"):
            span.set_attribute("policy.tool.risk", d["risk"])
        if result == "deny":
            span.add_event("policy.deny", {"stage": stage, "reason": d.get("reason", ""), "tool": extra.get("tool", ""), "model": extra.get("model", "") or ""})
        return d
