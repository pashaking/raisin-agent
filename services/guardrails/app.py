"""guardrails: the Guardrails Runtime service (replaces the archived LLM Guard API).

Three layers, each its own span, all fail closed:
  1. nemo        NVIDIA NeMo Guardrails (/v1/checks on the nemo-guardrails container): self-check input rail on
                 user messages and retrieved chunks, self-check output rail on drafts, injection detection
                 (YARA). The judge model is gpt-4o-mini via LiteLLM. Optional RULES_LAYER=on adds the regex
                 filters in rules.py as an extra deterministic layer.
  2. classifier  prompt-injection / jailbreak text classifier (default Meta Llama Prompt Guard 2 86M,
                 gated on Hugging Face -> HF_TOKEN; open fallback model if the download is refused)
  3. moderation  content-safety moderation on output via the LiteLLM gateway (/v1/moderations,
                 omni-moderation-latest), so the call is keyed, budgeted and traced like any model call

PII detection/anonymization stays in Presidio (separate containers, called by the agent runtime).
"""
import logging
import os
import re
import time

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from opentelemetry import trace
from pydantic import BaseModel

import rules
from otel_setup import init_tracing, instrument_fastapi

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("guardrails")
tracer = init_tracing("guardrails")
app = FastAPI(title="guardrails")
instrument_fastapi(app)

AUTH_TOKEN = os.environ.get("GUARDRAILS_AUTH_TOKEN", "")
INJECTION_MODEL = os.environ.get("INJECTION_MODEL", "meta-llama/Llama-Prompt-Guard-2-86M")
INJECTION_FALLBACK_MODEL = os.environ.get("INJECTION_FALLBACK_MODEL", "protectai/deberta-v3-base-prompt-injection-v2")
INJECTION_THRESHOLD = float(os.environ.get("INJECTION_THRESHOLD", "0.9"))
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
LITELLM_KEY = os.environ.get("LITELLM_KEY_GUARDRAILS", "")
MODERATION_MODEL = os.environ.get("MODERATION_MODEL", "omni-moderation-latest")
MODERATION_THRESHOLD = float(os.environ.get("MODERATION_THRESHOLD", "0.5"))
NEMO_URL = os.environ.get("NEMO_URL", "").rstrip("/")
NEMO_CONFIG_ID = os.environ.get("NEMO_CONFIG_ID", "aicp")
NEMO_MODEL = os.environ.get("NEMO_MODEL", "gpt-4o-mini")
RULES_LAYER = os.environ.get("RULES_LAYER", "off").lower() == "on"
POSITIVE_LABELS = {"INJECTION", "JAILBREAK", "MALICIOUS", "LABEL_1", "UNSAFE"}

STATE: dict = {"classifier": None, "model": None, "requested": INJECTION_MODEL, "fallback_used": False, "error": None}


# ---------- model loading ----------
def _load(model_id: str):
    from transformers import pipeline

    return pipeline("text-classification", model=model_id, top_k=None, truncation=True, max_length=512,
                    token=os.environ.get("HF_TOKEN") or None)


@app.on_event("startup")
def load_classifier():
    t0 = time.time()
    for candidate in (INJECTION_MODEL, INJECTION_FALLBACK_MODEL):
        try:
            STATE["classifier"] = _load(candidate)
            STATE["model"] = candidate
            STATE["fallback_used"] = candidate != INJECTION_MODEL
            STATE["classifier"]("warm-up")  # download + first inference before health goes green
            log.info("classifier ready model=%s fallback=%s in %.1fs", candidate, STATE["fallback_used"], time.time() - t0)
            return
        except Exception as e:  # noqa: BLE001
            STATE["error"] = f"{candidate}: {type(e).__name__}: {str(e)[:200]}"
            log.warning("classifier load failed for %s: %s", candidate, STATE["error"])
    log.error("no injection classifier available; service stays unhealthy (fail closed)")


def _positive_score(preds: list[dict]) -> tuple[float, str]:
    """Score of the 'attack' class regardless of how the model names its labels."""
    best = (0.0, preds[0]["label"] if preds else "?")
    for p in preds:
        if p["label"].upper() in POSITIVE_LABELS and p["score"] > best[0]:
            best = (p["score"], p["label"])
    return best


_SENT = re.compile(r"(?<=[.!?\n])\s+")
# Presidio placeholders the runtime substitutes before the scan. The injection classifiers were trained on natural text
# and read a bare EMAIL_ADDRESS_1 token as template/markup injection (deberta-v3 fallback: "email me at
# <EMAIL_ADDRESS_1>" scored 0.988 vs 0.002 for the literal address, 2026-09-07). The classifier therefore scores the
# text with the tokens swapped for neutral words; the NeMo judge and the rules layer still see the real tokens.
_PLACEHOLDER = re.compile(r"<(PERSON|EMAIL_ADDRESS|PHONE_NUMBER|CREDIT_CARD)_\d+>")
_PLACEHOLDER_WORDS = {"PERSON": "[name]", "EMAIL_ADDRESS": "[email]", "PHONE_NUMBER": "[phone]", "CREDIT_CARD": "[card]"}


def neutralize_placeholders(text: str) -> str:
    return _PLACEHOLDER.sub(lambda m: _PLACEHOLDER_WORDS[m.group(1)], text)


def classify(text: str) -> dict:
    """Whole-text score plus the max per-sentence score. Sentence granularity catches an injected paragraph
    buried in a benign document, but on procedural docs it also fires on ordinary imperative sentences, so the
    caller decides which score is authoritative per stage (see _injection_scan)."""
    clf = STATE["classifier"]
    if clf is None:
        raise HTTPException(503, {"error": "classifier not loaded", "detail": STATE["error"]})
    text = neutralize_placeholders(text)
    sentences = [x for x in _SENT.split(text) if len(x.split()) >= 3][:63]
    preds = clf([text, *sentences], batch_size=16)
    text_score, text_label = _positive_score(preds[0])
    sent_score, sent_label = 0.0, "?"
    for p in preds[1:]:
        sc, lb = _positive_score(p)
        if sc > sent_score:
            sent_score, sent_label = sc, lb
    return {"model": STATE["model"], "text_score": round(text_score, 4), "sentence_score": round(sent_score, 4),
            "label": text_label if text_score >= sent_score else sent_label, "threshold": INJECTION_THRESHOLD}


def moderate(text: str) -> dict:
    if not (LITELLM_BASE_URL and LITELLM_KEY):
        return {"enabled": False}
    with httpx.Client(timeout=20, headers={"Authorization": f"Bearer {LITELLM_KEY}"}) as c:
        r = c.post(f"{LITELLM_BASE_URL}/v1/moderations", json={"model": MODERATION_MODEL, "input": text})
    r.raise_for_status()
    res = r.json()["results"][0]
    scores = res.get("category_scores", {}) or {}
    over = sorted({k.replace("/", "_") for k, v in scores.items() if (v or 0) >= MODERATION_THRESHOLD}
                  | {k.replace("/", "_") for k, v in (res.get("categories") or {}).items() if v})
    return {"enabled": True, "model": MODERATION_MODEL, "flagged": bool(over), "categories": over,
            "max_score": round(max([0.0, *[v or 0 for v in scores.values()]]), 4)}


def nemo_check(messages: list[dict], rail_types: list[str]) -> dict:
    """NeMo Guardrails /v1/checks: runs only the requested rail types and reports passed / modified / blocked plus the
    blocking rail name. Unreachable or non-200 -> exception -> the caller fails closed."""
    body = {"model": NEMO_MODEL, "messages": messages, "guardrails": {"config_id": NEMO_CONFIG_ID, "rail_types": rail_types}}
    with httpx.Client(timeout=60) as c:
        r = c.post(f"{NEMO_URL}/v1/checks", json=body)
    r.raise_for_status()
    d = r.json()
    return {"status": d.get("status"), "rail": d.get("rail"), "config_id": NEMO_CONFIG_ID, "model": NEMO_MODEL}


def _nemo_span(messages: list[dict], rail_types: list[str], stage: str) -> dict:
    with tracer.start_as_current_span("guardrail.nemo") as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        span.set_attribute("guardrail.stage", stage)
        span.set_attribute("guardrail.nemo.rail_types", rail_types)
        n = nemo_check(messages, rail_types)
        span.set_attribute("guardrail.model", n["model"])
        span.set_attribute("guardrail.nemo.status", str(n["status"]))
        if n.get("rail"):
            span.set_attribute("guardrail.nemo.rail", n["rail"])
        span.set_attribute("guardrail.verdict", "pass" if n["status"] == "passed" else "hit")
        return n


# ---------- API ----------
class ScanReq(BaseModel):
    text: str
    prompt: str | None = None


def auth(authorization: str | None = Header(default=None)):
    if AUTH_TOKEN and authorization != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "bad guardrails token")


def _rules_span(text: str, ruleset, stage: str) -> list[str]:
    with tracer.start_as_current_span("guardrail.rules") as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        hits = rules.scan(text, ruleset)
        span.set_attribute("guardrail.stage", stage)
        span.set_attribute("guardrail.rules.hits", hits)
        span.set_attribute("guardrail.verdict", "hit" if hits else "pass")
        return hits


def _classifier_span(text: str, stage: str) -> dict:
    """input: max(text, sentence) is authoritative (short prompts, hidden paragraphs). retrieved: whole-text score is
    authoritative and the sentence score is reported only, because knowledge docs are full of imperative sentences
    that read as instructions; the rules layer is what pins an injected chunk."""
    with tracer.start_as_current_span("guardrail.classifier") as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        c = classify(text)
        c["score"] = max(c["text_score"], c["sentence_score"]) if stage == "input" else c["text_score"]
        c["unit"] = "text" if c["score"] == c["text_score"] else "sentence"
        span.set_attribute("guardrail.stage", stage)
        span.set_attribute("guardrail.model", c["model"])
        span.set_attribute("guardrail.risk_score", c["score"])
        span.set_attribute("guardrail.classifier.text_score", c["text_score"])
        span.set_attribute("guardrail.classifier.sentence_score", c["sentence_score"])
        span.set_attribute("guardrail.verdict", "hit" if c["score"] >= INJECTION_THRESHOLD else "pass")
        return c


def _moderation_span(text: str) -> dict:
    with tracer.start_as_current_span("guardrail.moderation") as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        m = moderate(text)
        span.set_attribute("guardrail.moderation.enabled", m.get("enabled", False))
        if m.get("enabled"):
            span.set_attribute("guardrail.model", m["model"])
            span.set_attribute("guardrail.risk_score", m["max_score"])
            span.set_attribute("guardrail.moderation.categories", m["categories"])
        span.set_attribute("guardrail.verdict", "hit" if m.get("flagged") else "pass")
        return m


def _injection_scan(req: ScanReq, stage: str) -> dict:
    n = _nemo_span([{"role": "user", "content": req.text}], ["input"], stage)
    hits = _rules_span(req.text, rules.INPUT_RULES, stage) if RULES_LAYER else []
    c = _classifier_span(req.text, stage)
    reasons = ([f"nemo:{n['rail']}"] if n["status"] == "blocked" else []) + [f"rule:{h}" for h in hits] \
        + ([f"classifier:{c['label']}"] if c["score"] >= INJECTION_THRESHOLD else [])
    risk = max(c["score"], 1.0 if (hits or n["status"] == "blocked") else 0.0)
    layers = {"nemo": n, "classifier": c}
    if RULES_LAYER:
        layers["rules"] = {"hits": hits}
    return {"verdict": "block" if reasons else "pass", "risk_score": round(risk, 4), "reason": reasons[0] if reasons else None,
            "reasons": reasons, "layers": layers}


@app.post("/v1/scan/input", dependencies=[Depends(auth)])
def scan_input(req: ScanReq):
    return _injection_scan(req, "input")


@app.post("/v1/scan/retrieved", dependencies=[Depends(auth)])
def scan_retrieved(req: ScanReq):
    return _injection_scan(req, "retrieved")


@app.post("/v1/scan/output", dependencies=[Depends(auth)])
def scan_output(req: ScanReq):
    msgs = [{"role": "user", "content": req.prompt or "(no prompt)"}, {"role": "assistant", "content": req.text}]
    n = _nemo_span(msgs, ["output"], "output")
    hits = _rules_span(req.text, rules.OUTPUT_RULES, "output") if RULES_LAYER else []
    m = _moderation_span(req.text)
    reasons = ([f"nemo:{n['rail']}"] if n["status"] == "blocked" else []) + [f"rule:{h}" for h in hits] \
        + [f"moderation:{c}" for c in m.get("categories", [])]
    risk = max(m.get("max_score", 0.0), 1.0 if (hits or n["status"] == "blocked") else 0.0)
    layers = {"nemo": n, "moderation": m}
    if RULES_LAYER:
        layers["rules"] = {"hits": hits}
    return {"verdict": "block" if reasons else "pass", "risk_score": round(risk, 4), "reason": reasons[0] if reasons else None,
            "reasons": reasons, "layers": layers}


@app.get("/healthz")
def healthz():
    if STATE["classifier"] is None:
        raise HTTPException(503, {"ok": False, "error": STATE["error"]})
    return {"ok": True, "injection_model": STATE["model"], "injection_model_requested": STATE["requested"],
            "fallback_used": STATE["fallback_used"], "injection_threshold": INJECTION_THRESHOLD,
            "moderation": {"enabled": bool(LITELLM_BASE_URL and LITELLM_KEY), "model": MODERATION_MODEL, "threshold": MODERATION_THRESHOLD},
            "nemo": {"url": NEMO_URL, "config_id": NEMO_CONFIG_ID, "judge_model": NEMO_MODEL},
            "rules_layer": RULES_LAYER,
            "rules": {"input": [r for r, _ in rules.INPUT_RULES], "output": [r for r, _ in rules.OUTPUT_RULES]} if RULES_LAYER else {}}
