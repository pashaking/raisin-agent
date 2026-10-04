package system.authz_test

import rego.v1

import data.system.authz

tokens := {"runtime": "rt-secret", "seed": "seed-secret"}

test_anonymous_denied if {
	not authz.allow with input as {"method": "GET", "path": ["v1", "data", "roles"], "identity": "anonymous"} with data.opa_tokens as tokens
}

test_health_open if {
	authz.allow with input as {"method": "GET", "path": ["health"], "identity": "anonymous"} with data.opa_tokens as tokens
}

test_runtime_decision_allowed if {
	authz.allow with input as {"method": "POST", "path": ["v1", "data", "aicp", "authz", "decision"], "identity": "rt-secret"} with data.opa_tokens as tokens
}

test_runtime_cannot_write_data if {
	not authz.allow with input as {"method": "PUT", "path": ["v1", "data", "registry"], "identity": "rt-secret"} with data.opa_tokens as tokens
}

test_runtime_cannot_read_roles if {
	not authz.allow with input as {"method": "GET", "path": ["v1", "data", "roles"], "identity": "rt-secret"} with data.opa_tokens as tokens
}

test_seed_can_put_registry_only if {
	authz.allow with input as {"method": "PUT", "path": ["v1", "data", "registry"], "identity": "seed-secret"} with data.opa_tokens as tokens
	not authz.allow with input as {"method": "PUT", "path": ["v1", "data", "roles"], "identity": "seed-secret"} with data.opa_tokens as tokens
}

test_nobody_uploads_policy if {
	not authz.allow with input as {"method": "PUT", "path": ["v1", "policies", "rogue"], "identity": "seed-secret"} with data.opa_tokens as tokens
	not authz.allow with input as {"method": "PUT", "path": ["v1", "policies", "rogue"], "identity": "rt-secret"} with data.opa_tokens as tokens
}
