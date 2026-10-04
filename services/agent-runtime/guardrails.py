"""Guardrails Runtime client: Presidio (PII) + guardrails service (rule filters, prompt-injection classifier,
output moderation).
Each hop is its own span; all three services fail closed (unreachable -> GuardrailUnavailable)."""
import re
from dataclasses import dataclass, field

import httpx
from opentelemetry import trace

import config

tracer = trace.get_tracer("agent-runtime.guardrails")

INPUT_ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD"]
OUTPUT_ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD"]
BLOCK_ENTITIES = {"EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD"}
# Presidio's phone recognizer scores 0.4 without context words ("phone", "call") and 0.75 with them, so the
# service is queried at 0.3 and per-entity thresholds are applied here. Transaction ids, dates and charity
# names never score as these four entity types (verified against the analyzer on 2026-09-06).
ANALYZE_THRESHOLD = 0.3
ENTITY_THRESHOLDS = {"PERSON": 0.6, "EMAIL_ADDRESS": 0.6, "PHONE_NUMBER": 0.4, "CREDIT_CARD": 0.6}
REFUSAL = "I can't share that response because it contained personal data that must not be disclosed. Please rephrase or contact support."


class GuardrailBlocked(Exception):
    def __init__(self, stage: str, detail: dict):
        super().__init__(stage)
        self.stage, self.detail = stage, detail


class GuardrailUnavailable(Exception):
    def __init__(self, service: str):
        super().__init__(service)
        self.service = service


@dataclass
class Placeholders:
    mapping: dict = field(default_factory=dict)  # "<PERSON_1>" -> "Jane Doe"
    types: dict = field(default_factory=dict)    # "<PERSON_1>" -> "PERSON"

    def restore(self, text: str) -> str:
        for ph, orig in self.mapping.items():
            text = text.replace(ph, orig)
        return text

    def social(self, text: str) -> str:
        """Public social post: PERSON -> first name only. Any sentence that carries a contact placeholder
        (phone, email, card) is dropped whole, so the post never reads "Contact me at or ."."""
        contact = [ph for ph, t in self.types.items() if t != "PERSON"]
        sentences = re.split(r"(?<=[.!?])\s+", text)
        text = " ".join(s for s in sentences if not any(ph in s for ph in contact))
        for ph, orig in self.mapping.items():
            if self.types[ph] == "PERSON":
                text = text.replace(ph, orig.split()[0] if orig.strip() else "")
            else:
                text = text.replace(ph, "")
        return " ".join(text.split())


# ---------- low-level clients ----------
def _presidio_analyze(text: str, entities: list[str]) -> list[dict]:
    with tracer.start_as_current_span("guardrail.presidio.analyze") as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        span.set_attribute("guardrail.service", "presidio-analyzer")
        try:
            with httpx.Client(timeout=config.GUARDRAIL_TIMEOUT_S) as c:
                r = c.post(f"{config.PRESIDIO_ANALYZER_URL}/analyze",
                           json={"text": text, "language": "en", "entities": entities, "score_threshold": ANALYZE_THRESHOLD})
            r.raise_for_status()
            res = [x for x in r.json() if x["score"] >= ENTITY_THRESHOLDS.get(x["entity_type"], 0.6)]
        except Exception as e:  # noqa: BLE001
            span.record_exception(e)
            span.set_attribute("guardrail.verdict", "unavailable")
            raise GuardrailUnavailable("presidio-analyzer") from e
        counts: dict[str, int] = {}
        for x in res:
            counts[x["entity_type"]] = counts.get(x["entity_type"], 0) + 1
        span.set_attribute("guardrail.entities", [f"{k}:{v}" for k, v in sorted(counts.items())])
        return res


def _presidio_anonymize(text: str, results: list[dict]) -> str:
    with tracer.start_as_current_span("guardrail.presidio.anonymize") as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        span.set_attribute("guardrail.service", "presidio-anonymizer")
        span.set_attribute("guardrail.entities_masked", len(results))
        try:
            with httpx.Client(timeout=config.GUARDRAIL_TIMEOUT_S) as c:
                r = c.post(f"{config.PRESIDIO_ANONYMIZER_URL}/anonymize",
                           json={"text": text, "anonymizers": {"DEFAULT": {"type": "replace", "new_value": "<REDACTED>"}},
                                 "analyzer_results": [{"entity_type": x["entity_type"], "start": x["start"], "end": x["end"], "score": x["score"]} for x in results]})
            r.raise_for_status()
            return r.json()["text"]
        except Exception as e:  # noqa: BLE001
            span.record_exception(e)
            span.set_attribute("guardrail.verdict", "unavailable")
            raise GuardrailUnavailable("presidio-anonymizer") from e


def _guardrails(path: str, payload: dict, span_name: str, fail_label: str = "block") -> tuple[bool, dict]:
    """One call to the guardrails service; its own layer spans (rules / classifier / moderation) nest under this one."""
    with tracer.start_as_current_span(span_name) as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        span.set_attribute("guardrail.service", "guardrails")
        try:
            with httpx.Client(timeout=config.GUARDRAIL_TIMEOUT_S, headers={"Authorization": f"Bearer {config.GUARDRAILS_AUTH_TOKEN}"}) as c:
                r = c.post(f"{config.GUARDRAILS_URL}{path}", json=payload)
            r.raise_for_status()
            body = r.json()
        except Exception as e:  # noqa: BLE001
            span.record_exception(e)
            span.set_attribute("guardrail.verdict", "unavailable")
            raise GuardrailUnavailable("guardrails") from e
        ok = body.get("verdict") == "pass"
        layers = body.get("layers", {}) or {}
        summary = {"verdict": body.get("verdict"), "risk_score": body.get("risk_score", 0.0), "reason": body.get("reason"),
                   "reasons": body.get("reasons", []), "rules": (layers.get("rules") or {}).get("hits", [])}
        if "nemo" in layers:
            summary["nemo"] = {k: layers["nemo"].get(k) for k in ("status", "rail")}
            span.set_attribute("guardrail.nemo.status", str(layers["nemo"].get("status")))
        if "classifier" in layers:
            summary["classifier"] = {k: layers["classifier"].get(k) for k in ("model", "score", "text_score", "sentence_score", "label", "unit")}
            span.set_attribute("guardrail.model", str(layers["classifier"].get("model")))
        if layers.get("moderation", {}).get("enabled"):
            summary["moderation"] = {k: layers["moderation"].get(k) for k in ("model", "flagged", "categories", "max_score")}
        span.set_attribute("guardrail.risk_score", float(summary["risk_score"] or 0.0))
        span.set_attribute("guardrail.rules.hits", summary["rules"])
        span.set_attribute("guardrail.reasons", summary["reasons"])
        span.set_attribute("guardrail.verdict", "pass" if ok else fail_label)
        return ok, summary


# ---------- stages ----------
def input_stage(text: str) -> tuple[str, Placeholders]:
    """(a) Presidio -> numbered placeholders kept in-process; (b) guardrails input scan (rules + injection classifier). Block -> raise."""
    with tracer.start_as_current_span("guardrail.input") as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        results = _presidio_analyze(text, INPUT_ENTITIES)
        ph = Placeholders()
        counters: dict[str, int] = {}
        # replace from the end so offsets stay valid; identical strings share one placeholder
        seen: dict[str, str] = {}
        sanitized = text
        for r in sorted(results, key=lambda x: x["start"], reverse=True):
            orig = text[r["start"]:r["end"]]
            if orig in seen:
                token = seen[orig]
            else:
                counters[r["entity_type"]] = counters.get(r["entity_type"], 0) + 1
                token = f"<{r['entity_type']}_{counters[r['entity_type']]}>"
                seen[orig] = token
                ph.mapping[token] = orig
                ph.types[token] = r["entity_type"]
            sanitized = sanitized[:r["start"]] + token + sanitized[r["end"]:]
        span.set_attribute("guardrail.placeholders", len(ph.mapping))
        ok, summary = _guardrails("/v1/scan/input", {"text": sanitized}, "guardrail.scan.prompt")
        if not ok:
            span.set_attribute("guardrail.verdict", "block")
            span.add_event("guardrail.block", {"stage": "input", "reasons": summary["reasons"]})
            raise GuardrailBlocked("input", summary)
        span.set_attribute("guardrail.verdict", "pass")
        return sanitized, ph


def retrieved_stage(chunks: list[dict]) -> int:
    """Injection scan (rules + classifier) over each retrieved chunk. Flags, never drops: downstream controls
    (policy on tool calls, output scan) are what must hold."""
    with tracer.start_as_current_span("guardrail.retrieved") as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        flagged = 0
        for ch in chunks:
            ok, summary = _guardrails("/v1/scan/retrieved", {"text": ch["content"]}, "guardrail.scan.retrieved_chunk", fail_label="flagged")
            ch["flagged"] = not ok
            ch["flag_reasons"] = summary["reasons"]
            flagged += 0 if ok else 1
        span.set_attribute("guardrail.chunks", len(chunks))
        span.set_attribute("guardrail.flagged", flagged)
        span.set_attribute("guardrail.verdict", "flagged" if flagged else "pass")
        if flagged:
            span.add_event("guardrail.flagged", {"stage": "retrieved", "chunks": [c["doc_id"] for c in chunks if c.get("flagged")]})
        return flagged


def output_stage(draft: str, ph: Placeholders, role: str, prompt_for_guard: str) -> tuple[str, dict]:
    """(a) Presidio: EMAIL/PHONE/CC not among this request's placeholders -> block; PERSON -> flagged.
    (b) guardrails output scan: secret/exfil rules + content moderation. (c) Restore per role; Donor gets anonymizer masking instead."""
    with tracer.start_as_current_span("guardrail.output") as span:
        span.set_attribute("openinference.span.kind", "GUARDRAIL")
        results = _presidio_analyze(draft, OUTPUT_ENTITIES)
        blockers = [r for r in results if r["entity_type"] in BLOCK_ENTITIES]
        persons = [r for r in results if r["entity_type"] == "PERSON"]
        info = {"presidio_block": len(blockers), "presidio_flagged_person": len(persons)}
        if blockers:
            span.set_attribute("guardrail.verdict", "block")
            span.add_event("guardrail.block", {"stage": "output", "entities": [b["entity_type"] for b in blockers]})
            return REFUSAL, {**info, "verdict": "block", "reason": "pii_in_output"}
        ok, summary = _guardrails("/v1/scan/output", {"text": draft, "prompt": prompt_for_guard}, "guardrail.scan.output")
        info["guardrails"] = summary
        if not ok:
            span.set_attribute("guardrail.verdict", "block")
            span.add_event("guardrail.block", {"stage": "output", "reasons": summary["reasons"]})
            return REFUSAL, {**info, "verdict": "block", "reason": summary["reason"] or "guardrails_output"}
        if role == "Donor":
            # Donors never get placeholders restored; the anonymizer is always consulted on this path so its
            # availability is part of the control (fail closed), even when nothing needs masking.
            final = _presidio_anonymize(draft, persons)
            verdict = "flagged" if persons else "pass"
        else:
            final = ph.restore(draft)
            verdict = "flagged" if persons else "pass"
        span.set_attribute("guardrail.verdict", verdict)
        return final, {**info, "verdict": verdict}
