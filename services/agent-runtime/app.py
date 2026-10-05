"""Agent runtime: the half of the "AI Gateway" box that LiteLLM does not do.
Runs the tool loop; asks OPA before choosing a model and before every tool call; calls the guardrail
services on input, retrieved chunks, and output; emits one span per hop.
"""
import json
import time

from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace
from pydantic import BaseModel

import approvals
import config
import guardrails
import llm
import policy
import tools
from auth import verify_bearer
from limits import TERMINATION_MESSAGES, detect_loop
from otel_setup import current_trace_id, init_tracing, instrument_fastapi

tracer = init_tracing("agent-runtime")
app = FastAPI(title="agent-runtime")
instrument_fastapi(app)


class RunReq(BaseModel):
    question: str
    model: str | None = None
    temperature: float | None = None
    session_id: str | None = None  # caller-supplied continuity key for get_session_context/save_task_state (Phase 8)


class StoryReq(BaseModel):
    free_text: str
    model: str | None = None


class DecideReq(BaseModel):
    decision: str  # "approve" | "reject"


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
        ctx = {"claims": claims, "authorization": authorization, "decisions": [], "guardrail_info": {}, "session_id": req.session_id}
        # Phase 13: the Finance role gets the Operations Agent persona (transaction/campaign investigation);
        # every other role gets the donor-facing support copilot. Each is a distinct, stable instruction contract.
        prompt_name = "operations-agent-v1" if claims["role"] == "Finance" else "assistant-system-v1"
        root.set_attribute("gen_ai.prompt.name", prompt_name)
        root.set_attribute("agent.session.id", req.session_id or "")

        d = _model_stage(claims, prompt_name, req.model, ctx)
        model, allowed = d["model"], d.get("allowed_tools", [])
        ctx["retrieval_filter"] = d.get("retrieval_filter", {"tenants": [claims["tenant_id"], "platform"], "classifications": ["platform"]})
        ctx["client"] = _gateway_client(claims)

        sanitized, ph = _guard_input(req.question)
        ctx["placeholders"] = ph  # tools resolve <EMAIL_ADDRESS_1>-style arguments server-side; the model never sees the value
        ctx["guardrail_info"]["input"] = {"placeholders": len(ph.mapping)}

        messages = [{"role": "system", "content": config.PROMPTS[prompt_name]}, {"role": "user", "content": sanitized}]
        schemas = tools.schemas_for(allowed)
        tool_log = []
        answer = ""
        run_start = time.monotonic()
        total_tool_calls = 0
        total_tokens = 0
        call_names: list[str] = []
        fail_counts: dict[tuple[str, str], int] = {}
        termination_reason = "complete"
        steps_used = 0
        try:
            for step in range(config.MAX_TOOL_ROUNDS):
                steps_used = step + 1
                if time.monotonic() - run_start > config.MAX_RUNTIME_S:
                    termination_reason = "max_runtime"
                    break
                if total_tokens >= config.MAX_TOKENS_PER_RUN:
                    termination_reason = "max_tokens"
                    break
                msg, _resp = llm.chat(ctx["client"], model, messages, schemas or None, req.temperature)
                if _resp.usage:
                    total_tokens += _resp.usage.total_tokens
                if not msg.tool_calls:
                    answer = msg.content or ""
                    break
                messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    if time.monotonic() - run_start > config.MAX_RUNTIME_S:
                        termination_reason = "max_runtime"
                        break
                    if total_tool_calls >= config.MAX_TOOL_CALLS:
                        termination_reason = "max_tool_calls"
                        break
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    key = (tc.function.name, json.dumps(args, sort_keys=True, default=str))
                    if fail_counts.get(key, 0) >= config.MAX_RETRIES_PER_TOOL:
                        result = {"error": "retry_limit_exceeded", "message": "This exact call has failed too many times; try a different approach."}
                    else:
                        try:
                            result = tools.execute(tc.function.name, args, ctx)
                        except guardrails.GuardrailUnavailable as e:
                            _err(400, error="guardrail_unavailable", service=e.service, verdict="unavailable")
                        if result.get("error"):
                            fail_counts[key] = fail_counts.get(key, 0) + 1
                    total_tool_calls += 1
                    call_names.append(tc.function.name)
                    entry = {"tool": tc.function.name, "args": args, "outcome": result.get("error", "ok")}
                    if result.get("approval_id"):
                        entry["approval_id"] = result["approval_id"]
                    tool_log.append(entry)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
                if termination_reason != "complete":
                    break
                if detect_loop(call_names):
                    termination_reason = "repeated_tool_execution"
                    break
            else:
                termination_reason = "max_steps"
            if termination_reason != "complete" and not answer:
                answer = TERMINATION_MESSAGES[termination_reason]
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            root.record_exception(e)
            _err(502, error="gateway_error", message=f"{type(e).__name__}: {str(e)[:300]}")
        root.set_attribute("termination.reason", termination_reason)
        root.set_attribute("agent.step", steps_used)
        root.set_attribute("agent.tool_calls", total_tool_calls)
        root.set_attribute("agent.tokens_used", total_tokens)

        try:
            final, ginfo = guardrails.output_stage(answer, ph, claims["role"], sanitized)
        except guardrails.GuardrailUnavailable as e:
            _err(400, error="guardrail_unavailable", service=e.service, verdict="unavailable")
        ctx["guardrail_info"]["output"] = ginfo
        return _envelope(
            {"answer": final, "trace_id": current_trace_id()},
            {"policy": {"model": model, "allowed_tools": allowed, "withheld_tools": d.get("withheld_tools", []),
                        "registry_revision": d.get("registry_revision"), "decisions": ctx["decisions"]},
             "tool_calls": tool_log, "guardrails": ctx["guardrail_info"],
             "run": {"steps": steps_used, "tool_calls": total_tool_calls, "tokens_used": total_tokens,
                     "runtime_s": round(time.monotonic() - run_start, 2), "termination_reason": termination_reason}})


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


def _require_role(claims: dict, role: str):
    if claims["role"] != role:
        raise HTTPException(403, {"error": "forbidden", "message": f"requires the {role} role"})


@app.get("/approvals")
def list_approvals(authorization: str | None = Header(default=None)):
    """Pending approval queue, scoped to the caller's own tenant. Finance only (this POC's one business role) --
    a direct role check, not an OPA call: this lists a queue and executes nothing. Same "second, independent layer"
    pattern raisin-api's own /api/* routes use (see docs/containers/opa.md); the OPA call that actually matters is
    approval_execute, below, right before anything runs."""
    claims = verify_bearer(authorization)
    _require_role(claims, "Finance")
    return {"pending": approvals.list_pending(claims["tenant_id"])}


@app.get("/approvals/{approval_id}")
def get_approval(approval_id: str, authorization: str | None = Header(default=None)):
    """The original requester can check their own request's status regardless of role; any other viewer must be a
    same-tenant Finance approver."""
    claims = verify_bearer(authorization)
    row = approvals.get(approval_id)
    if row is None or row["tenant_id"] != claims["tenant_id"]:
        raise HTTPException(404, {"error": "not_found"})
    if claims["sub"] != row["requested_by"] and claims["role"] != "Finance":
        raise HTTPException(403, {"error": "forbidden"})
    return row


@app.post("/approvals/{approval_id}/decide")
def decide_approval(approval_id: str, req: DecideReq, authorization: str | None = Header(default=None)):
    """Approval integrity: this endpoint takes no tool/args from the request, only a decision on an id. A reject is
    terminal. An approve re-validates OPA's separate approval_execute stage (role/tool/tenant/risk, fresh -- not just
    trusting that the row says 'pending') and then runs the exact stored tool/args via tools.execute_approved."""
    with tracer.start_as_current_span("approval.decide") as span:
        claims = verify_bearer(authorization)
        _require_role(claims, "Finance")
        if req.decision not in ("approve", "reject"):
            raise HTTPException(422, {"error": "bad_args", "message": "decision must be approve or reject"})
        row = approvals.get(approval_id)
        span.set_attribute("approval.id", approval_id)
        if row is None or row["tenant_id"] != claims["tenant_id"]:
            raise HTTPException(404, {"error": "not_found"})
        if row["status"] != "pending":
            raise HTTPException(409, {"error": "already_decided", "status": row["status"]})
        span.set_attribute("approval.tool", row["tool"])
        span.set_attribute("approval.decision", req.decision)
        if req.decision == "reject":
            approvals.mark(approval_id, "rejected", claims["sub"])
            return {"approval_id": approval_id, "status": "rejected"}
        d = policy.decide("approval_execute", claims, tool=row["tool"], resource={"tenant_id": row["tenant_id"]})
        span.set_attribute("policy.result", "allow" if d.get("allow") else "deny")
        if not d.get("allow"):
            approvals.mark(approval_id, "failed", claims["sub"], result={"error": "policy_deny", "reason": d.get("reason")})
            raise HTTPException(403, {"error": "policy_deny", "reason": d.get("reason"), "decision_id": d.get("decision_id")})
        result = tools.execute_approved(row["tool"], row["args"], {"authorization": authorization})
        status = "executed" if "error" not in result else "failed"
        approvals.mark(approval_id, status, claims["sub"], result=result)
        return {"approval_id": approval_id, "status": status, "result": result}


@app.get("/health")
def health():
    return {"ok": True, "trace_content": "on" if config.TRACE_CONTENT else "off", "debug_panel": "on" if config.DEBUG_PANEL else "off"}
