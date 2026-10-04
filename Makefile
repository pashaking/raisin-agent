SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: help pull build up up-gate gate check-gate seed demo trace test test-tools opa-test opa-query opa-data registry-flip registry-reset ps logs down clean

help:
	@echo "make up            build + start all 13 services (waits for health; guardrails model download is minutes on first start)"
	@echo "make seed          schema, LiteLLM keys, registry -> OPA, embed knowledge docs"
	@echo "make demo          run scenarios 0-9 and print trace links   (make demo S='1 3' for a subset)"
	@echo "make trace TRACE_ID=<id>   print one trace as a step-by-step tree with the prompt/tool/policy attributes per hop (ARGS=--full)"
	@echo "make test          opa unit tests + PCI-scope + PII-canary + gateway-content trace assertions + data-tool API assertions (no LLM)"
	@echo "make test-tools    data tools end to end: raisin-api endpoints, NeMo rail regression, runtime tool loop (calls gpt-4o-mini)"
	@echo "make test-fail-closed   same plus stop/start of opa and guardrail services"
	@echo "make registry-flip TOOL=get_donation APPROVED=false | make registry-reset"
	@echo "make up-gate / gate / check-gate TRACE_ID=..   hour-one risk gate only"
	@echo "make ps / logs / down / clean"

pull:
	$(COMPOSE) --profile seed --profile gate pull --ignore-buildable

build:
	$(COMPOSE) --profile seed --profile gate build

up: build
	@test -f .env || (echo "copy .env.example to .env and fill in keys" && exit 1)
	$(COMPOSE) up -d --wait

up-gate:
	@test -f .env || (echo "copy .env.example to .env and fill in keys" && exit 1)
	$(COMPOSE) up -d --wait postgres jaeger phoenix otel-collector litellm

gate:
	$(COMPOSE) run --rm gate

check-gate:
	python3 scripts/check_gate.py $(TRACE_ID)

seed:
	$(COMPOSE) run --rm seed

demo:
	python3 scripts/demo.py $(S)

trace:
	python3 scripts/trace_walk.py $(TRACE_ID) $(ARGS)

opa-test:
	docker run --rm -v "$(PWD)/policy:/policy:ro" openpolicyagent/opa:1.20.2 test /policy/authz.rego /policy/authz_test.rego /policy/system_authz.rego /policy/system_authz_test.rego -v

# OPA is token-authenticated and not on the host. Ask it from inside the runtime (decision) or the seed job (data).
#   make opa-query INPUT='{"stage":"tool","tool":"get_donation","subject":{"sub":"finance.b@charity-b.test","role":"Finance","tenant_id":"tenant-b"},"resource":{"tenant_id":"tenant-a"}}'
#   make opa-data PATH=roles/Donor/tools
opa-query:
	$(COMPOSE) exec -T agent-runtime python -c 'import httpx,os,sys,json;print(json.dumps(httpx.post(os.environ["OPA_URL"]+"/v1/data/aicp/authz/decision",headers={"Authorization":"Bearer "+os.environ["OPA_TOKEN_RUNTIME"]},json={"input":json.loads(sys.argv[1])}).json(),indent=2))' '$(INPUT)'

opa-data:
	$(COMPOSE) run --rm seed python -c 'import httpx,os,sys,json;print(json.dumps(httpx.get(os.environ["OPA_URL"]+"/v1/data/"+sys.argv[1],headers={"Authorization":"Bearer "+os.environ["OPA_TOKEN_SEED"]}).json(),indent=2))' '$(PATH)'

test: opa-test
	python3 scripts/test_traces.py
	python3 scripts/test_tools.py --no-llm

test-tools:
	python3 scripts/test_tools.py

test-fail-closed: opa-test
	python3 scripts/test_traces.py --fail-closed

registry-flip:
	$(COMPOSE) run --rm seed python seed.py flip $(TOOL) $(APPROVED)

registry-reset:
	$(COMPOSE) run --rm seed python seed.py reset

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=100

down:
	$(COMPOSE) --profile seed --profile gate down

clean:
	$(COMPOSE) --profile seed --profile gate down -v
