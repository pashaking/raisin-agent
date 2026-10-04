"""Hardcoded fixtures. Three tenants + platform, six personas, seeded donations/transactions, canary donor,
registry rows. Everything here is synthetic (.test domains, 555 phone)."""

TENANTS = [("tenant-a", "Charity A"), ("tenant-b", "Charity B"), ("tenant-c", "PMCF"), ("platform", "Raisin platform")]

USERS = [
    ("finance.a@charity-a.test", "tenant-a", "Finance"),
    ("finance.b@charity-b.test", "tenant-b", "Finance"),
    ("donor.b@example.test", "tenant-b", "Donor"),
    ("participant.a@example.test", "tenant-a", "Participant"),
    ("admin@aka.com", "tenant-c", "Finance"),
    ("donor@pmcf.ca", "tenant-c", "Donor"),
]

# Never allowed to appear in any span attribute or event (make test asserts this).
CANARY_EMAIL = "canary.donor@pii-canary.test"
CANARY_PHONE = "604-555-0199"

# Presidio's email recognizer validates the TLD, so reserved test TLDs (.test) are never placeholdered. The canary
# stays on .test on purpose (it must never appear in a span); donors that demos look up by email use example.com.
DONORS = [  # (tenant, email, phone, name)
    ("tenant-a", CANARY_EMAIL, CANARY_PHONE, "Canary Donor"),
    ("tenant-a", "alex.a@example.com", None, "Alex Andersen"),
    ("tenant-a", "morgan.a@example.com", "604-555-0111", "Morgan Adler"),
    ("tenant-b", "regular.donor@example.test", None, "Regular Donor"),
    ("tenant-b", "donor.b@example.test", None, "Donor B"),  # same email as the Donor persona -> get_my_donations
    ("tenant-c", "donor@pmcf.ca", None, "Donor C"),  # same email as the tenant-c Donor persona -> get_my_donations
    ("tenant-c", "paul@aka.com", None, "paul uchitel"),
    ("tenant-c", "sam.c@example.com", "604-555-0133", "Sam Chen"),
    ("tenant-c", "jordan.c@example.com", None, "Jordan Cole"),
    ("tenant-c", "taylor.c@example.com", "604-555-0144", "Taylor Cruz"),  # recurring donor: five approved donations
]

DONATIONS = [  # (id, tenant, donor_email, amount, currency, status)
    (873928, "tenant-a", CANARY_EMAIL, 250.00, "CAD", "declined"),
    (873901, "tenant-a", CANARY_EMAIL, 100.00, "CAD", "approved"),
    (873902, "tenant-a", "alex.a@example.com", 40.00, "CAD", "approved"),
    (873903, "tenant-a", "alex.a@example.com", 25.00, "CAD", "declined"),
    (873904, "tenant-a", "morgan.a@example.com", 500.00, "CAD", "approved"),
    (991204, "tenant-b", "regular.donor@example.test", 80.00, "CAD", "approved"),
    (991201, "tenant-b", "regular.donor@example.test", 60.00, "CAD", "approved"),
    (991202, "tenant-b", "donor.b@example.test", 30.00, "CAD", "declined"),
    (991203, "tenant-b", "donor.b@example.test", 120.00, "CAD", "approved"),
    (12345, "tenant-c", "paul@aka.com", 25.00, "CAD", "approved"),
    (774101, "tenant-c", "donor@pmcf.ca", 75.00, "CAD", "approved"),
    (774102, "tenant-c", "donor@pmcf.ca", 20.00, "CAD", "declined"),
    (774103, "tenant-c", "sam.c@example.com", 150.00, "CAD", "approved"),
    (774104, "tenant-c", "sam.c@example.com", 45.00, "CAD", "declined"),
    (774105, "tenant-c", "jordan.c@example.com", 1000.00, "CAD", "approved"),
    (774106, "tenant-c", "jordan.c@example.com", 60.00, "CAD", "declined"),
    (774107, "tenant-c", "taylor.c@example.com", 50.00, "CAD", "approved"),
    (774108, "tenant-c", "taylor.c@example.com", 50.00, "CAD", "approved"),
    (774109, "tenant-c", "taylor.c@example.com", 50.00, "CAD", "approved"),
    (774110, "tenant-c", "taylor.c@example.com", 200.00, "CAD", "approved"),
    (774111, "tenant-c", "taylor.c@example.com", 50.00, "CAD", "approved"),
]

TRANSACTIONS = [  # (id, donation_id, tenant, amount, currency, result, decline_code, fraud_score)
    (873928, 873928, "tenant-a", 250.00, "CAD", "declined", "51", 0.12),
    (873901, 873901, "tenant-a", 100.00, "CAD", "approved", None, 0.04),
    (873902, 873902, "tenant-a", 40.00, "CAD", "approved", None, 0.03),
    (873903, 873903, "tenant-a", 25.00, "CAD", "declined", "05", 0.31),
    (873904, 873904, "tenant-a", 500.00, "CAD", "approved", None, 0.02),
    (991204, 991204, "tenant-b", 80.00, "CAD", "approved", None, 0.05),
    (991201, 991201, "tenant-b", 60.00, "CAD", "approved", None, 0.05),
    (991202, 991202, "tenant-b", 30.00, "CAD", "declined", "14", 0.22),
    (991203, 991203, "tenant-b", 120.00, "CAD", "approved", None, 0.06),
    (873909, 12345, "tenant-c", 25.00, "CAD", "approved", None, 0.04),  # txn id != donation id: hand-added, kept as-is
    (774101, 774101, "tenant-c", 75.00, "CAD", "approved", None, 0.04),
    (774102, 774102, "tenant-c", 20.00, "CAD", "declined", "51", 0.15),
    (774103, 774103, "tenant-c", 150.00, "CAD", "approved", None, 0.03),
    (774104, 774104, "tenant-c", 45.00, "CAD", "declined", "05", 0.28),
    (774105, 774105, "tenant-c", 1000.00, "CAD", "approved", None, 0.07),
    (774106, 774106, "tenant-c", 60.00, "CAD", "declined", "14", 0.19),
    (774107, 774107, "tenant-c", 50.00, "CAD", "approved", None, 0.02),
    (774108, 774108, "tenant-c", 50.00, "CAD", "approved", None, 0.02),
    (774109, 774109, "tenant-c", 50.00, "CAD", "approved", None, 0.03),
    (774110, 774110, "tenant-c", 200.00, "CAD", "approved", None, 0.05),
    (774111, 774111, "tenant-c", 50.00, "CAD", "approved", None, 0.02),
]

REGISTRY = [  # (kind, name, owner, risk_tier, approved)
    ("model", "gpt-4o-mini", "platform-eng", 2, True),
    ("model", "claude-sonnet", "platform-eng", 2, True),
    ("model", "text-embedding-3-small", "platform-eng", 1, True),
    ("tool", "get_transaction_analysis", "finance-systems", 3, True),
    ("tool", "search_kb", "platform-eng", 1, True),
    # data tools (read-only, minimized; tenant always from the JWT, never a tool argument)
    ("tool", "get_donation", "finance-systems", 3, True),
    ("tool", "list_transactions", "finance-systems", 3, True),
    ("tool", "get_tenant_donation_summary", "finance-systems", 2, True),
    ("tool", "get_donor_profile", "donor-data", 3, True),
    ("tool", "find_donor", "donor-data", 3, True),
    ("tool", "get_my_donations", "support", 2, True),
    ("tool", "resend_receipt", "support", 3, True),       # approved but in no role -> always denied
    ("tool", "legacy_summarizer", "none", 2, False),        # unapproved -> opa test case
    ("prompt", "assistant-system-v1", "platform-eng", 2, True),
    ("prompt", "story-generator-v1", "product", 2, True),
]

# LiteLLM virtual keys: alias -> (env var holding the key value, max_budget USD, budget_duration)
KEYS = {
    "tenant-a-finance": ("LITELLM_KEY_TENANT_A_FINANCE", 10.0, "1d"),
    "tenant-b-finance": ("LITELLM_KEY_TENANT_B_FINANCE", 10.0, "1d"),
    "tenant-c-finance": ("LITELLM_KEY_TENANT_C_FINANCE", 5.0, "1d"),
    "story-generator": ("LITELLM_KEY_STORY_GENERATOR", 10.0, "1d"),
    "seed-indexer": ("LITELLM_KEY_SEED_INDEXER", 5.0, "1d"),
}
# keys whose model access differs from the default (gpt-4o-mini, claude-sonnet, text-embedding-3-small)
KEY_MODELS = {"guardrails": ["omni-moderation-latest", "gpt-4o-mini"]}  # moderation + NeMo self-check judge
KEYS["guardrails"] = ("LITELLM_KEY_GUARDRAILS", 5.0, "1d")

# doc file -> (tenant_id, classification)
DOCS = {
    "refund-policy.md": ("platform", "platform"),
    "tax-receipt-policy.md": ("platform", "platform"),
    "decline-codes-runbook.md": ("platform", "platform"),
    "donor-faq.md": ("platform", "platform"),
    "tenant-a/finance-procedure.md": ("tenant-a", "tenant-internal"),
    "tenant-a/campaign-guide.md": ("tenant-a", "tenant-internal"),
    "tenant-a/poisoned-finance-procedure.md": ("tenant-a", "tenant-internal"),
    
    "tenant-b/finance-procedure.md": ("tenant-b", "tenant-internal"),
    "tenant-b/campaign-guide.md": ("tenant-b", "tenant-internal"),

    "tenant-c/campaign-management-policy.md": ("tenant-c", "tenant-internal"),
    "tenant-c/donation-acceptance-policy.md": ("tenant-c", "tenant-internal"),
    "tenant-c/Donor Recognition Policy.md": ("tenant-c", "tenant-internal"),
    "tenant-c/donor-privacy-policy.md": ("tenant-c", "tenant-internal"),
    "tenant-c/Information Security Policy.md": ("tenant-c", "tenant-internal"),
    "tenant-c/Payment Security Policy.md": ("tenant-c", "tenant-internal"),
    "tenant-c/Refund Policy.md": ("tenant-c", "tenant-internal"),

    "incident-runbook.md": ("platform", "internal"),
}
