"""Agent runtime: the half of the "AI Gateway" box that LiteLLM does not do.
Runs the tool loop; asks OPA before choosing a model and before every tool call; calls the guardrail
services on input, retrieved chunks, and output; emits one span per hop.
"""
import json

from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace
from pydantic import BaseModel

import config
import guardrails
import llm
import policy
import tools
from auth import verify_bearer
from otel_setup import current_trace_id, init_tracing, instrument_fastapi

tracer = init_tracing("agent-runtime")
app = FastAPI(title="agent-runtime")
instrument_fastapi(app)


class RunReq(BaseModel):
    question: str
    model: str | None = None
    temperature: float | None = None


class StoryReq(BaseModel):
    free_text: str
    model: str | None = None


# Keys an end user may see on a 4xx/5xx when DEBUG_PANEL is off. Everything else (guardrail scores, rail names, classifier
# output, OPA decision ids, raw exception text) is an oracle for tuning attacks (CSO F5) and stays in the trace only.
PUBLIC_ERR_KEYS = {"error", "stage", "reason", "verdict", "service", "trace_id"}


def _err(status: int, **detail):
    detail["trace_id"] = current_trace_id()
    if not config.DEBUG_PANEL:
        detail = {k: v for k, v in detail.items() if k in PUBLIC_ERR_KEYS}
        if detail.get("error") == "gateway_error":
            detail["reason"] = "upstream_error"
    raise HTTPException(status, detail)


def _envelope(public: dict, panel: dict) -> dict:
    """Response minimization (CSO F5/F6): end users get the answer and a trace id; policy decisions, tool outcomes and
    guardrail detail are for the console and only with DEBUG_PANEL=on."""
    return {**public, **panel} if config.DEBUG_PANEL else public


def _gateway_client(claims: dict):
    key = config.GATEWAY_KEYS.get((claims["tenant_id"], claims["role"]), "")
    if not key:
        _err(403, error="no_gateway_key", message="no LiteLLM virtual key mapped for this tenant/role")
    return llm.client_for(key)


def _model_stage(claims: dict, prompt_name: str, model: str | None, ctx: dict) -> dict:
    d = policy.decide("model", claims, model=model or "", prompt=prompt_name)
    ctx["decisions"].append({"stage": "model", "allow": d.get("allow"), "reason": d.get("reason"), "model": d.get("model")})
    if not d.get("allow"):
        _err(403, error="policy_deny", stage="model", reason=d.get("reason"), decision_id=d.get("decision_id"))
    return d


def _guard_input(text: str):
    try:
        return guardrails.input_stage(text)
    except guardrails.GuardrailBlocked as e:
        _err(400, error="guardrail_block", stage="input", detail=e.detail)
    except guardrails.GuardrailUnavailable as e:
        _err(400, error="guardrail_unavailable", service=e.service, verdict="unavailable")


@app.post("/run")
def run(req: RunReq, authorization: str | None = Header(default=None)):
    with tracer.start_as_current_span("assistant.run") as root:
        root.set_attribute("openinference.span.kind", "AGENT")
        claims = verify_bearer(authorization)
        ctx = {"claims": claims, "authorization": authorization, "decisions": [], "guardrail_info": {}}
        root.set_attribute("gen_ai.prompt.name", "assistant-system-v1")

        d = _model_stage(claims, "assistant-system-v1", req.model, ctx)
        model, allowed = d["model"], d.get("allowed_tools", [])
        ctx["retrieval_filter"] = d.get("retrieval_filter", {"tenants": [claims["tenant_id"], "platform"], "classifications": ["platform"]})
        ctx["client"] = _gateway_client(claims)

        sanitized, ph = _guard_input(req.question)
        ctx["placeholders"] = ph  # tools resolve <EMAIL_ADDRESS_1>-style arguments server-side; the model never sees the value
        ctx["guardrail_info"]["input"] = {"placeholders": len(ph.mapping)}

        messages = [{"role": "system", "content": config.PROMPTS["assistant-system-v1"]}, {"role": "user", "content": sanitized}]
        schemas = tools.schemas_for(allowed)
        tool_log = []
        answer = ""
        try:
            for _ in range(config.MAX_TOOL_ROUNDS):
                msg, _resp = llm.chat(ctx["client"], model, messages, schemas or None, req.temperature)
                if not msg.tool_calls:
                    answer = msg.content or ""
                    break
                messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    try:
                        result = tools.execute(tc.function.name, args, ctx)
                    except guardrails.GuardrailUnavailable as e:
                        _err(400, error="guardrail_unavailable", service=e.service, verdict="unavailable")
                    tool_log.append({"tool": tc.function.name, "args": args, "outcome": result.get("error", "ok")})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
            else:
                answer = "I could not complete the request within the allowed number of steps."
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            root.record_exception(e)
            _err(502, error="gateway_error", message=f"{type(e).__name__}: {str(e)[:300]}")

        try:
            final, ginfo = guardrails.output_stage(answer, ph, claims["role"], sanitized)
        except guardrails.GuardrailUnavailable as e:
            _err(400, error="guardrail_unavailable", service=e.service, verdict="unavailable")
        ctx["guardrail_info"]["output"] = ginfo
        return _envelope(
            {"answer": final, "trace_id": current_trace_id()},
            {"policy": {"model": model, "allowed_tools": allowed, "withheld_tools": d.get("withheld_tools", []),
                        "registry_revision": d.get("registry_revision"), "decisions": ctx["decisions"]},
             "tool_calls": tool_log, "guardrails": ctx["guardrail_info"]})


@app.post("/story")
def story(req: StoryReq, authorization: str | None = Header(default=None)):
    with tracer.start_as_current_span("story.run") as root:
        root.set_attribute("openinference.span.kind", "CHAIN")
        claims = verify_bearer(authorization)
        ctx = {"claims": claims, "authorization": authorization, "decisions": [], "guardrail_info": {}}
        root.set_attribute("gen_ai.prompt.name", "story-generator-v1")
        d = _model_stage(claims, "story-generator-v1", req.model, ctx)
        model = d["model"]
        ctx["client"] = _gateway_client(claims)
        sanitized, ph = _guard_input(req.free_text)
        ctx["guardrail_info"]["input"] = {"placeholders": len(ph.mapping), "tokens": sorted(ph.mapping.keys())}
        messages = [{"role": "system", "content": config.PROMPTS["story-generator-v1"]}, {"role": "user", "content": sanitized}]
        try:
            msg, _ = llm.chat(ctx["client"], model, messages, None, 0.7)
        except Exception as e:  # noqa: BLE001
            root.record_exception(e)
            _err(502, error="gateway_error", message=f"{type(e).__name__}: {str(e)[:300]}")
        raw = msg.content or ""
        try:
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            story_txt, social = str(data.get("story", "")), str(data.get("social_post", ""))
        except Exception:  # noqa: BLE001
            story_txt, social = raw, ""
        try:
            checked, ginfo = guardrails.output_stage(story_txt + "\n\n" + social, ph, claims["role"], sanitized)
        except guardrails.GuardrailUnavailable as e:
            _err(400, error="guardrail_unavailable", service=e.service, verdict="unavailable")
        ctx["guardrail_info"]["output"] = ginfo
        if ginfo.get("verdict") == "block":
            return _envelope({"story": checked, "social_post": "", "trace_id": current_trace_id()},
                             {"policy": {"model": model, "decisions": ctx["decisions"]}, "guardrails": ctx["guardrail_info"]})
        return _envelope(
            {"story": ph.restore(story_txt), "social_post": ph.social(social), "trace_id": current_trace_id()},
            {"model_saw": {"placeholders": sorted(ph.mapping.keys())},
             "policy": {"model": model, "decisions": ctx["decisions"], "registry_revision": d.get("registry_revision")},
             "guardrails": ctx["guardrail_info"]})


@app.get("/health")
def health():
    return {"ok": True, "trace_content": "on" if config.TRACE_CONTENT else "off", "debug_panel": "on" if config.DEBUG_PANEL else "off"}
