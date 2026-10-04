# OPA's own API authorization (CSO F3). OPA runs with --authentication=token --authorization=basic, so every
# REST call carries a bearer token that arrives here as input.identity. Tokens live in /opa-tokens/tokens.json,
# rendered by docker compose from .env (OPA_TOKEN_RUNTIME, OPA_TOKEN_SEED). The PDP is no longer writable by
# anything that happens to be on the compose network.
package system.authz

import rego.v1

default allow := false

# liveness for compose / tests
allow if input.path == ["health"]

# agent-runtime: ask for decisions, nothing else (cannot read roles, cannot change data or policy)
allow if {
	input.identity == data.opa_tokens.runtime
	input.method == "POST"
	input.path == ["v1", "data", "aicp", "authz", "decision"]
}

# seed: publish the registry document, and read data for RUNBOOK inspection; never policies
allow if {
	input.identity == data.opa_tokens.seed
	input.method == "PUT"
	input.path == ["v1", "data", "registry"]
}

allow if {
	input.identity == data.opa_tokens.seed
	input.method == "GET"
	input.path[0] == "v1"
	input.path[1] == "data"
}
