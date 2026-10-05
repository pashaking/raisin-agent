package aicp.authz_test

import rego.v1

import data.aicp.authz

roles := {
	"Finance": {"models": ["claude-sonnet", "gpt-4o-mini"], "tools": ["get_transaction_analysis", "get_donation", "list_transactions", "get_tenant_donation_summary", "get_donor_profile", "find_donor", "search_kb"], "classifications": ["platform", "tenant-internal"]},
	"Donor": {"models": ["gpt-4o-mini"], "tools": ["search_kb", "get_my_donations"], "classifications": ["platform"]},
	"Participant": {"models": ["claude-sonnet", "gpt-4o-mini"], "tools": [], "classifications": []},
}

registry := {
	"models": {"gpt-4o-mini": {"approved": true}, "claude-sonnet": {"approved": true}},
	"tools": {
		"get_transaction_analysis": {"approved": true, "action_risk": "low"},
		"get_donation": {"approved": true, "action_risk": "low"},
		"list_transactions": {"approved": true, "action_risk": "low"},
		"get_tenant_donation_summary": {"approved": true, "action_risk": "low"},
		"get_donor_profile": {"approved": true, "action_risk": "medium"},
		"find_donor": {"approved": true, "action_risk": "medium"},
		"get_my_donations": {"approved": true, "action_risk": "low"},
		"search_kb": {"approved": true, "action_risk": "low"},
		"resend_receipt": {"approved": true, "action_risk": "high"},
		"legacy_summarizer": {"approved": false, "action_risk": "medium"},
		# transaction-investigation tool set (Phase 3): not in the base `roles` fixture's Finance.tools above (so the
		# model-stage allowed_tools exact-equality tests keep passing unmodified); tests below grant them per-test via
		# object.union, the same pattern the resend_receipt tests use.
		"get_campaign": {"approved": true, "action_risk": "low"},
		"get_campaign_statistics": {"approved": true, "action_risk": "low"},
		"get_transaction": {"approved": true, "action_risk": "low"},
		"search_transactions": {"approved": true, "action_risk": "low"},
		"get_transaction_statistics": {"approved": true, "action_risk": "low"},
		"compare_transaction_periods": {"approved": true, "action_risk": "low"},
		"get_decline_statistics": {"approved": true, "action_risk": "low"},
		"get_payment_gateway_statistics": {"approved": true, "action_risk": "low"},
		"get_fraud_signals": {"approved": true, "action_risk": "low"},
		"get_application_errors": {"approved": true, "action_risk": "low"},
		"get_incident_history": {"approved": true, "action_risk": "low"},
		# controlled actions (plan Phase 14): not in the base `roles` fixture's Finance.tools either, granted per-test
		# via object.union exactly like the Phase 3 set above.
		"restart_worker": {"approved": true, "action_risk": "medium"},
		"clear_failed_job": {"approved": true, "action_risk": "medium"},
		"block_ip_temporarily": {"approved": true, "action_risk": "medium"},
		"create_incident": {"approved": true, "action_risk": "low"},
		"send_notification": {"approved": true, "action_risk": "low"},
		"collect_diagnostic_bundle": {"approved": true, "action_risk": "low"},
	},
	"prompts": {"assistant-system-v1": {"approved": true}, "story-generator-v1": {"approved": true}},
	"revision": "test",
}

finance_a := {"sub": "finance.a@charity-a.test", "tenant_id": "tenant-a", "role": "Finance"}

finance_b := {"sub": "finance.b@charity-b.test", "tenant_id": "tenant-b", "role": "Finance"}

donor_b := {"sub": "donor.b@example.test", "tenant_id": "tenant-b", "role": "Donor"}

test_finance_model_allow_default_model if {
	d := authz.decision with input as {"stage": "model", "subject": finance_a, "prompt": "assistant-system-v1"}
		with data.roles as roles with data.registry as registry
	d.allow
	d.model == "claude-sonnet"
	d.allowed_tools == ["get_transaction_analysis", "get_donation", "list_transactions", "get_tenant_donation_summary", "get_donor_profile", "find_donor", "search_kb"]
	# withheld_tools is every approved tool the role doesn't list (model-stage coarse layer): the registry fixture
	# above also carries the Phase 3 investigation tools as approved, un-granted to this base `roles` fixture's
	# Finance -- so they show up here too, not just resend_receipt/get_my_donations.
	{t | some t in d.withheld_tools} == {"resend_receipt", "get_my_donations", "get_campaign", "get_campaign_statistics",
		"get_transaction", "search_transactions", "get_transaction_statistics", "compare_transaction_periods",
		"get_decline_statistics", "get_payment_gateway_statistics", "get_fraud_signals", "get_application_errors",
		"get_incident_history", "restart_worker", "clear_failed_job", "block_ip_temporarily", "create_incident",
		"send_notification", "collect_diagnostic_bundle"}
	d.retrieval_filter.tenants == ["tenant-a", "platform"]
}

test_finance_model_explicit_allowed if {
	d := authz.decision with input as {"stage": "model", "subject": finance_a, "model": "gpt-4o-mini", "prompt": "assistant-system-v1"}
		with data.roles as roles with data.registry as registry
	d.allow
	d.model == "gpt-4o-mini"
}

test_donor_model_allow_withholds_transaction_tool if {
	d := authz.decision with input as {"stage": "model", "subject": donor_b, "prompt": "assistant-system-v1"}
		with data.roles as roles with data.registry as registry
	d.allow
	d.allowed_tools == ["search_kb", "get_my_donations"]
	"get_transaction_analysis" in d.withheld_tools
	"find_donor" in d.withheld_tools
	"get_donor_profile" in d.withheld_tools
	"list_transactions" in d.withheld_tools
}

test_donor_tool_deny_donor_profile_not_in_role if {
	d := authz.decision with input as {"stage": "tool", "subject": donor_b, "tool": "get_donor_profile", "resource": {"tenant_id": "tenant-b"}}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "tool_not_in_role"
}

test_donor_tool_allow_my_donations_self if {
	d := authz.decision with input as {"stage": "tool", "subject": donor_b, "tool": "get_my_donations", "resource": {"tenant_id": "tenant-b"}}
		with data.roles as roles with data.registry as registry
	d.allow
}

test_finance_tool_deny_get_donation_foreign_tenant if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_b, "tool": "get_donation", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "tenant_mismatch"
}

test_finance_tool_allow_find_donor_same_tenant if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "find_donor", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as registry
	d.allow
	d.risk == "medium"
}

test_donor_model_not_in_role_denied if {
	d := authz.decision with input as {"stage": "model", "subject": donor_b, "model": "claude-sonnet", "prompt": "assistant-system-v1"}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "model_not_in_role"
}

test_unapproved_prompt_denied if {
	d := authz.decision with input as {"stage": "model", "subject": finance_a, "prompt": "rogue-prompt"}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "unapproved_prompt"
}

test_unknown_role_denied if {
	d := authz.decision with input as {"stage": "model", "subject": {"sub": "x", "tenant_id": "tenant-a", "role": "Hacker"}, "prompt": "assistant-system-v1"}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "unknown_role"
}

test_tool_allow_same_tenant if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "get_transaction_analysis", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as registry
	d.allow
}

test_tool_deny_tenant_mismatch if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_b, "tool": "get_transaction_analysis", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "tenant_mismatch"
}

test_tool_deny_not_in_role_resend_receipt if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "resend_receipt", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "tool_not_in_role"
}

test_tool_deny_unapproved_registry_entry if {
	roles2 := object.union(roles, {"Finance": {"models": ["claude-sonnet"], "tools": ["legacy_summarizer"], "classifications": []}})
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "legacy_summarizer", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles2 with data.registry as registry
	not d.allow
	d.reason == "unapproved_tool"
}

test_registry_flip_denies_previously_allowed_tool if {
	reg2 := object.union(registry, {"tools": object.union(registry.tools, {"get_transaction_analysis": {"approved": false, "action_risk": "low"}})})
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "get_transaction_analysis", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as reg2
	not d.allow
	d.reason == "unapproved_tool"
}

# Phase 9: risk classification. high/critical action_risk requires human sign-off; the tool stage itself never
# grants it, so it denies with a reason distinct from an ordinary policy deny instead of auto-executing. Phase 10's
# approval_execute stage (tests below) is the only path that can allow one, and only post-decision.
test_tool_deny_requires_approval_high_risk if {
	roles2 := object.union(roles, {"Finance": object.union(roles.Finance, {"tools": array.concat(roles.Finance.tools, ["resend_receipt"])})})
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "resend_receipt", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles2 with data.registry as registry
	not d.allow
	d.reason == "requires_approval"
	d.risk == "high"
}

test_tool_deny_requires_approval_outranks_nothing_when_tenant_mismatch if {
	# a cross-tenant request on a high-risk tool must still read as tenant_mismatch, never requires_approval
	roles2 := object.union(roles, {"Finance": object.union(roles.Finance, {"tools": array.concat(roles.Finance.tools, ["resend_receipt"])})})
	d := authz.decision with input as {"stage": "tool", "subject": finance_b, "tool": "resend_receipt", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles2 with data.registry as registry
	not d.allow
	d.reason == "tenant_mismatch"
}

test_tool_allow_low_risk_carries_risk_field if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "get_transaction_analysis", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as registry
	d.allow
	d.risk == "low"
}

# ---- Phase 10: approval_execute stage. Only the agent runtime's approval-decide path calls this, never the agent's
# own tool-call loop -- re-validates role/tool/tenant/risk fresh at decision time, exactly like the tool stage.
finance_a_with_resend := object.union(roles, {"Finance": object.union(roles.Finance, {"tools": array.concat(roles.Finance.tools, ["resend_receipt"])})})

test_approval_execute_allow_matching_role_tenant_risk if {
	d := authz.decision with input as {"stage": "approval_execute", "subject": finance_a, "tool": "resend_receipt", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as finance_a_with_resend with data.registry as registry
	d.allow
	d.stage == "approval_execute"
	d.risk == "high"
}

test_approval_execute_deny_tenant_mismatch if {
	# the approver's own tenant must match the resource's -- a tenant-b Finance user can never approve tenant-a's request
	d := authz.decision with input as {"stage": "approval_execute", "subject": finance_b, "tool": "resend_receipt", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as finance_a_with_resend with data.registry as registry
	not d.allow
	d.reason == "tenant_mismatch"
}

test_approval_execute_deny_tool_not_in_role if {
	# roles (no resend_receipt grant) -- even a same-tenant Finance approver can't execute a tool their role never listed
	d := authz.decision with input as {"stage": "approval_execute", "subject": finance_a, "tool": "resend_receipt", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "tool_not_in_role"
}

test_approval_execute_deny_not_requires_approval_for_low_risk_tool if {
	# misuse guard: the approval stage refuses to execute a tool that was never gated by approval in the first place
	d := authz.decision with input as {"stage": "approval_execute", "subject": finance_a, "tool": "get_donation", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "not_requires_approval"
}

test_approval_execute_deny_unknown_role if {
	d := authz.decision with input as {"stage": "approval_execute", "subject": {"sub": "x", "tenant_id": "tenant-a", "role": "Hacker"}, "tool": "resend_receipt", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as finance_a_with_resend with data.registry as registry
	not d.allow
	d.reason == "unknown_role"
}

# ---- Phase 3: transaction-investigation tool set (literal plan tool names). All action_risk=low in the registry
# fixture above -- read-only investigation tools auto-execute once role/approval/tenant checks pass (Phase 1 autonomy:
# READ+ANALYZE+RECOMMEND only), same as any other low-risk tool; no new rego logic, just new data.
roles_with_investigation_tools := object.union(roles, {"Finance": object.union(roles.Finance, {"tools": array.concat(
	roles.Finance.tools,
	["get_campaign", "get_campaign_statistics", "get_transaction", "search_transactions", "get_transaction_statistics",
		"compare_transaction_periods", "get_decline_statistics", "get_payment_gateway_statistics", "get_fraud_signals",
		"get_application_errors", "get_incident_history"],
)})})

test_tool_allow_get_campaign_same_tenant if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "get_campaign", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles_with_investigation_tools with data.registry as registry
	d.allow
	d.risk == "low"
}

test_tool_deny_get_campaign_foreign_tenant if {
	# campaign ABC (tenant-a) asked about by a tenant-b Finance user -- same uniform tenant_mismatch every other by-id tool gets
	d := authz.decision with input as {"stage": "tool", "subject": finance_b, "tool": "get_campaign", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles_with_investigation_tools with data.registry as registry
	not d.allow
	d.reason == "tenant_mismatch"
}

test_tool_allow_get_transaction_statistics_own_tenant if {
	# aggregate tool (Phase 5/9 worked example): resource tenant is always the caller's own, like get_tenant_donation_summary
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "get_transaction_statistics", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles_with_investigation_tools with data.registry as registry
	d.allow
	d.risk == "low"
}

test_tool_allow_get_payment_gateway_statistics_own_tenant if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "get_payment_gateway_statistics", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles_with_investigation_tools with data.registry as registry
	d.allow
	d.risk == "low"
}

# ---- Phase 14: controlled actions. medium, like low, auto-executes once role/tenant checks pass (Phase 9 graduated
# autonomy: only high/critical ever reach requires_approval) -- same rego logic as find_donor/get_donor_profile above,
# just new data.
roles_with_controlled_actions := object.union(roles, {"Finance": object.union(roles.Finance, {"tools": array.concat(
	roles.Finance.tools,
	["restart_worker", "clear_failed_job", "block_ip_temporarily", "create_incident", "send_notification", "collect_diagnostic_bundle"],
)})})

test_tool_allow_restart_worker_same_tenant_medium_risk_auto_executes if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "restart_worker", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles_with_controlled_actions with data.registry as registry
	d.allow
	d.risk == "medium"
}

test_tool_allow_block_ip_temporarily_same_tenant_medium_risk_auto_executes if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "block_ip_temporarily", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles_with_controlled_actions with data.registry as registry
	d.allow
	d.risk == "medium"
}

test_tool_allow_create_incident_same_tenant_low_risk if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "create_incident", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles_with_controlled_actions with data.registry as registry
	d.allow
	d.risk == "low"
}

test_tool_deny_clear_failed_job_foreign_tenant if {
	d := authz.decision with input as {"stage": "tool", "subject": finance_b, "tool": "clear_failed_job", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles_with_controlled_actions with data.registry as registry
	not d.allow
	d.reason == "tenant_mismatch"
}

test_tool_deny_investigation_tool_not_in_role_when_ungranted if {
	# the base `roles` fixture (no investigation tools granted) must still deny -- these are additive, not default-on
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "get_fraud_signals", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as registry
	not d.allow
	d.reason == "tool_not_in_role"
}
