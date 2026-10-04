"""Deterministic filter layer: regex rules that fire before (and independently of) any model.

Same idea as NeMo Guardrails' YARA-based injection detection: cheap, explainable, versioned in git,
and they catch the structural signals a classifier can miss (hidden unicode, fake chat markers,
secrets, exfiltration URLs). Every hit is reported by rule id so the trace explains itself.
"""
import re

# ---------- input / retrieved content: prompt-injection shapes ----------
INPUT_RULES: list[tuple[str, re.Pattern]] = [
    ("instruction_override", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|system|all|any)\b.{0,30}"
        r"\b(instructions?|prompts?|rules?|guidelines?|policies|policy)\b", re.I | re.S)),
    ("system_prompt_exfil", re.compile(
        r"\b(print|reveal|show|repeat|output|display|dump|leak|tell me)\b.{0,40}"
        r"\b(system prompt|hidden prompt|initial prompt|your (instructions|prompt|rules)|developer message)\b", re.I | re.S)),
    ("role_hijack", re.compile(
        r"\b(you are now|act as|pretend (to be|you are)|from now on,? you|enter)\b.{0,60}"
        r"\b(unrestricted|without (any )?(rules|restrictions|limits)|jailbroken|DAN|developer mode|god mode)\b", re.I | re.S)),
    ("tool_coercion", re.compile(
        r"\b(you must|always|immediately|silently|secretly|first)\b.{0,40}\b(call|invoke|use|run|execute)\b.{0,40}"
        r"\b(tool|function|resend_receipt|get_transaction_analysis|search_kb)\b", re.I | re.S)),
    ("exfil_instruction", re.compile(
        r"\b(send|email|forward|post|upload|transmit)\b.{0,60}\b(results?|data|records?|details|transactions?|donor)\b.{0,40}"
        r"\b(to|at)\b.{0,10}([\w.+-]+@[\w-]+\.[\w.]+|https?://)", re.I | re.S)),
    ("fake_chat_marker", re.compile(
        r"(^|\n)\s*(\[?(system|assistant|developer)\]?\s*:|<\|?(im_start|system|assistant)\|?>|###\s*(system|instruction)s?\b)", re.I)),
    ("hidden_unicode", re.compile("[\u200b-\u200f\u2060-\u2064\ufeff]|[\U000E0000-\U000E007F]")),
    ("encoded_payload", re.compile(r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{4}){24,}={0,2}(?![A-Za-z0-9+/])")),
]

# ---------- model output: secrets and exfiltration channels ----------
OUTPUT_RULES: list[tuple[str, re.Pattern]] = [
    ("secret_aws_access_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret_openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("secret_jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("secret_private_key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----")),
    ("secret_bearer_token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("secret_assignment", re.compile(r"\b(api[_-]?key|secret|access[_-]?token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+]{16,}", re.I)),
    ("exfil_markdown_image", re.compile(r"!\[[^\]]*\]\(\s*https?://[^)\s]*[?&][^)\s]+\)")),
    ("exfil_url_with_payload", re.compile(r"https?://[^\s)\]]+[?&](data|q|payload|token|email|phone|card|pan|cvv)=[^\s)&]{6,}", re.I)),
    ("hidden_unicode", re.compile("[\u200b-\u200f\u2060-\u2064\ufeff]|[\U000E0000-\U000E007F]")),
]


def scan(text: str, rules: list[tuple[str, re.Pattern]]) -> list[str]:
    return [rule_id for rule_id, rx in rules if rx.search(text)]
