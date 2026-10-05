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

# Transaction-investigation tool set (plan Phase 3, literal tool names): campaigns + gateway/fraud/error/incident
# telemetry, scoped by tenant like everything else. Kept separate from the donor/donation fixtures above so those
# rows are untouched; occurred_at/started_at are relative to seed time (minutes/hours ago) so "last_hour" vs
# "previous_hour" demo queries show a real pattern regardless of when `make seed` runs.
CAMPAIGNS = [  # (id, tenant, name, status, goal_amount)
    (1, "tenant-a", "Campaign ABC", "active", 20000.00),
    (2, "tenant-c", "PMCF Annual Gala", "active", 50000.00),
]

# (id, tenant, campaign_id, amount, gateway, result, decline_code, fraud_score, minutes_ago)
# Campaign ABC (tenant-a): the previous hour is healthy across both gateways; in the last hour, "adyen" starts
# failing hard (decline_code 96 = processor_error) while "stripe" stays healthy and fraud scores stay low throughout
# -- Phase 5's worked example ("likely payment-gateway degradation, not card-testing"). PMCF Annual Gala (tenant-c):
# a card-testing burst in the last hour -- many small amounts, high fraud scores, one gateway, invalid_card declines
# -- the contrast case get_fraud_signals and the card_testing eval scenario need.
CARD_TESTING_IP = "203.0.113.50"  # TEST-NET-3 (RFC 5737): synthetic, same convention as the .test email domains

# (id, tenant, campaign_id, amount, gateway, result, decline_code, fraud_score, minutes_ago, ip_address|None)
CAMPAIGN_TRANSACTIONS = [
    (960001, "tenant-a", 1, 50.00, "stripe", "approved", None, 0.03, 110, None),
    (960002, "tenant-a", 1, 75.00, "adyen", "approved", None, 0.04, 105, None),
    (960003, "tenant-a", 1, 40.00, "stripe", "approved", None, 0.02, 95, None),
    (960004, "tenant-a", 1, 60.00, "adyen", "approved", None, 0.05, 90, None),
    (960005, "tenant-a", 1, 30.00, "stripe", "declined", "51", 0.14, 75, None),
    (960006, "tenant-a", 1, 45.00, "adyen", "approved", None, 0.03, 65, None),
    (960007, "tenant-a", 1, 55.00, "stripe", "approved", None, 0.03, 50, None),
    (960008, "tenant-a", 1, 65.00, "adyen", "declined", "96", 0.08, 45, None),
    (960009, "tenant-a", 1, 80.00, "adyen", "declined", "96", 0.07, 40, None),
    (960010, "tenant-a", 1, 35.00, "stripe", "approved", None, 0.02, 30, None),
    (960011, "tenant-a", 1, 70.00, "adyen", "declined", "96", 0.09, 20, None),
    (960012, "tenant-a", 1, 90.00, "adyen", "declined", "96", 0.06, 10, None),
    (960013, "tenant-a", 1, 25.00, "stripe", "approved", None, 0.03, 5, None),
    (960021, "tenant-c", 2, 500.00, "stripe", "approved", None, 0.05, 180, None),
    # card-testing cluster (plan Phase 9/10/14 worked example): small-amount, high-fraud-score, declined invalid_card
    # transactions all from one source IP -- what get_fraud_signals surfaces and block_ip_temporarily acts on.
    (960022, "tenant-c", 2, 5.00, "stripe", "declined", "14", 0.91, 18, CARD_TESTING_IP),
    (960023, "tenant-c", 2, 5.00, "stripe", "declined", "14", 0.93, 17, CARD_TESTING_IP),
    (960024, "tenant-c", 2, 5.00, "stripe", "declined", "14", 0.95, 16, CARD_TESTING_IP),
    (960025, "tenant-c", 2, 5.00, "stripe", "declined", "14", 0.92, 15, CARD_TESTING_IP),
    (960026, "tenant-c", 2, 5.00, "stripe", "approved", None, 0.88, 14, CARD_TESTING_IP),
]

# (tenant, service, error_type, message, minutes_ago)
APPLICATION_ERRORS = [
    ("tenant-a", "payment-gateway", "GatewayTimeoutError", "adyen charge request timed out after 8s", 42),
    ("tenant-a", "payment-gateway", "GatewayTimeoutError", "adyen charge request timed out after 11s", 22),
    ("tenant-a", "payment-gateway", "Gateway5xxError", "adyen returned 503 for /charge", 9),
    ("tenant-c", "agent-runtime", "ToolRetryExceeded", "search_kb exceeded retry budget for session abc123", 200),
]

# (tenant, title, status, severity, summary, started_minutes_ago, resolved_minutes_ago|None)
INCIDENTS = [
    ("tenant-a", "Adyen elevated decline rate", "open", "medium",
     "Adyen gateway returning elevated processor_error declines for Campaign ABC; Stripe unaffected.", 50, None),
    ("tenant-c", "Resolved: KB indexer lag", "resolved", "low",
     "search_kb results were briefly stale after a doc update; re-index fixed it.", 1440, 1380),
]

REGISTRY = [  # (kind, name, owner, risk_tier, action_risk, approved)
    ("model", "gpt-4o-mini", "platform-eng", 2, None, True),
    ("model", "claude-sonnet", "platform-eng", 2, None, True),
    ("model", "text-embedding-3-small", "platform-eng", 1, None, True),
    ("tool", "get_transaction_analysis", "finance-systems", 3, "low", True),
    ("tool", "search_kb", "platform-eng", 1, "low", True),
    # data tools (read-only, minimized; tenant always from the JWT, never a tool argument)
    ("tool", "get_donation", "finance-systems", 3, "low", True),
    ("tool", "list_transactions", "finance-systems", 3, "low", True),
    ("tool", "get_tenant_donation_summary", "finance-systems", 2, "low", True),
    ("tool", "get_donor_profile", "donor-data", 3, "medium", True),   # identifies one donor, even if fields are minimized
    ("tool", "find_donor", "donor-data", 3, "medium", True),         # donor lookup by email
    ("tool", "get_my_donations", "support", 2, "low", True),
    # write-ish action (sends an email); action_risk=high -> OPA requires_approval even if a role ever lists it
    ("tool", "resend_receipt", "support", 3, "high", True),
    ("tool", "legacy_summarizer", "none", 2, "medium", False),        # unapproved -> opa test case
    # controlled agent memory (Phase 8): get_* read-only, save_task_state is the only model-writable path
    ("tool", "get_user_context", "platform-eng", 1, "low", True),
    ("tool", "get_session_context", "platform-eng", 1, "low", True),
    ("tool", "save_task_state", "platform-eng", 1, "medium", True),
    # transaction-investigation tool set (Phase 3): all read-only, action_risk=low (Phase 1 autonomy: READ+ANALYZE+RECOMMEND only)
    ("tool", "get_campaign", "finance-systems", 3, "low", True),
    ("tool", "get_campaign_statistics", "finance-systems", 2, "low", True),
    ("tool", "get_transaction", "finance-systems", 3, "low", True),
    ("tool", "search_transactions", "finance-systems", 2, "low", True),
    ("tool", "get_transaction_statistics", "finance-systems", 2, "low", True),
    ("tool", "compare_transaction_periods", "finance-systems", 2, "low", True),
    ("tool", "get_decline_statistics", "finance-systems", 2, "low", True),
    ("tool", "get_payment_gateway_statistics", "finance-systems", 2, "low", True),
    ("tool", "get_fraud_signals", "finance-systems", 2, "low", True),
    ("tool", "get_application_errors", "platform-eng", 2, "low", True),
    ("tool", "get_incident_history", "platform-eng", 2, "low", True),
    # controlled actions (plan Phase 14): graduated autonomy per Phase 9 -- medium auto-executes once role/tenant
    # checks pass (reversible, scoped mitigations); low auto-executes too (informational/escalation, no side effect
    # on production traffic). Neither tier ever reaches the approval workflow; only high/critical do (resend_receipt).
    ("tool", "restart_worker", "platform-eng", 3, "medium", True),
    ("tool", "clear_failed_job", "platform-eng", 3, "medium", True),
    ("tool", "block_ip_temporarily", "finance-systems", 3, "medium", True),
    ("tool", "create_incident", "platform-eng", 2, "low", True),
    ("tool", "send_notification", "platform-eng", 2, "low", True),
    ("tool", "collect_diagnostic_bundle", "platform-eng", 2, "low", True),
    ("prompt", "assistant-system-v1", "platform-eng", 2, None, True),
    ("prompt", "operations-agent-v1", "platform-eng", 1, None, True),
    ("prompt", "story-generator-v1", "product", 2, None, True),
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

# Seeded "preference" memory (Phase 8): kind=preference rows are never written by the agent (no save tool exists
# for them), only read back through get_user_context. (tenant, user_id, key, value)
MEMORY_PREFS = [
    ("tenant-a", "finance.a@charity-a.test", "digest_frequency", {"value": "daily"}),
]

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
