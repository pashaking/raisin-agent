import os

LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"].rstrip("/")
OPA_URL = os.environ["OPA_URL"].rstrip("/")
OPA_TOKEN = os.environ.get("OPA_TOKEN_RUNTIME", "")  # bearer for OPA's own API; may only POST decisions (system.authz)
GUARDRAILS_URL = os.environ["GUARDRAILS_URL"].rstrip("/")
GUARDRAILS_AUTH_TOKEN = os.environ.get("GUARDRAILS_AUTH_TOKEN", "")
PRESIDIO_ANALYZER_URL = os.environ["PRESIDIO_ANALYZER_URL"].rstrip("/")
PRESIDIO_ANONYMIZER_URL = os.environ["PRESIDIO_ANONYMIZER_URL"].rstrip("/")
RAISIN_API_URL = os.environ["RAISIN_API_URL"].rstrip("/")
RUNTIME_SERVICE_TOKEN = os.environ["RUNTIME_SERVICE_TOKEN"]
KB_DATABASE_URL = os.environ["KB_DATABASE_URL"]

# Demo/debug switches. Both default OFF (secure default); .env turns them on for the local console and Phoenix prompt playback.
#   DEBUG_TRACE_CONTENT=on  -> LLM spans carry full message bodies / tool args (otherwise: structure + sha256 only)   [CSO F1]
#   DEBUG_PANEL=on          -> API responses carry policy decisions, tool outcomes, guardrail detail (otherwise: answer + trace_id) [CSO F5/F6]
TRACE_CONTENT = os.environ.get("DEBUG_TRACE_CONTENT", "off").lower() == "on"
DEBUG_PANEL = os.environ.get("DEBUG_PANEL", "off").lower() == "on"

POLICY_TIMEOUT_S = 2.0
GUARDRAIL_TIMEOUT_S = 30.0
MAX_TOOL_ROUNDS = 4
EMBED_MODEL = "text-embedding-3-small"

# (tenant, role) -> LiteLLM virtual key. Missing mapping = no gateway access (fail closed).
GATEWAY_KEYS = {
    ("tenant-a", "Finance"): os.environ.get("LITELLM_KEY_TENANT_A_FINANCE", ""),
    ("tenant-b", "Finance"): os.environ.get("LITELLM_KEY_TENANT_B_FINANCE", ""),
    ("tenant-b", "Donor"): os.environ.get("LITELLM_KEY_TENANT_B_FINANCE", ""), 
    ("tenant-c", "Finance"): os.environ.get("LITELLM_KEY_TENANT_C_FINANCE", ""),
    ("tenant-c", "Donor"): os.environ.get("LITELLM_KEY_TENANT_C_FINANCE", ""),   # shared tenant budget
    ("tenant-a", "Participant"): os.environ.get("LITELLM_KEY_STORY_GENERATOR", ""),
}

PROMPTS = {
    "assistant-system-v1": (
        "You are the Raisin support copilot for charity administrators. Answer questions about donations, "
        "declines, receipts, donors and finance procedures for the administrator's own charity. Use the tools for any "
        "donation, transaction or donor fact; never guess amounts, counts or statuses. Use search_kb for policies and "
        "procedures. Placeholder tokens such as <EMAIL_ADDRESS_1> in the user's message stand for real values; pass them "
        "to tools unchanged as arguments and refer to the donor only by the donor id the tool returns, never by email. "
        "Never disclose donor personal data (names, emails, phone numbers). If a tool returns an error with "
        "reason denied or tenant_mismatch, tell the user they are not authorized for that record and stop; if it "
        "returns not_found, say the record was not found for their charity. Keep answers under 120 words."
    ),
    "story-generator-v1": (
        "You write fundraising content for a peer-to-peer campaign participant. Given the participant's notes, "
        "return strict JSON with two keys: \"story\" (a warm first-person story page of 120-180 words) and "
        "\"social_post\" (one social media post under 280 characters). Keep any placeholder tokens such as "
        "<PERSON_1> or <PHONE_NUMBER_1> exactly as written. The social post is public: never include phone "
        "numbers, email addresses, or their placeholder tokens in it. Return only JSON."
    ),
}
