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
		"get_transaction_analysis": {"approved": true},
		"get_donation": {"approved": true},
		"list_transactions": {"approved": true},
		"get_tenant_donation_summary": {"approved": true},
		"get_donor_profile": {"approved": true},
		"find_donor": {"approved": true},
		"get_my_donations": {"approved": true},
		"search_kb": {"approved": true},
		"resend_receipt": {"approved": true},
		"legacy_summarizer": {"approved": false},
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
	{t | some t in d.withheld_tools} == {"resend_receipt", "get_my_donations"}
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
	reg2 := object.union(registry, {"tools": object.union(registry.tools, {"get_transaction_analysis": {"approved": false}})})
	d := authz.decision with input as {"stage": "tool", "subject": finance_a, "tool": "get_transaction_analysis", "resource": {"tenant_id": "tenant-a"}}
		with data.roles as roles with data.registry as reg2
	not d.allow
	d.reason == "unapproved_tool"
}
