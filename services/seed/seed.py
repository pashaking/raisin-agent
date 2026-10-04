"""Seed job. Each unit runs under its own root span so seed traces never mix with demo traces.

  python seed.py all        schema -> keys -> index -> registry
  python seed.py registry   regenerate policy/data/registry.json from ai_registry and push to OPA
  python seed.py flip <tool> <true|false>   change approved flag, then registry
  python seed.py reset      restore fixture approvals, then registry
"""
import hashlib
import json
import os
import pathlib
import sys
import time

import httpx
import psycopg
from opentelemetry import trace

import fixtures as F
from otel_setup import flush, init_tracing

tracer = init_tracing("seed")

RAISIN_DB = os.environ["RAISIN_DATABASE_URL"]
KB_DB = os.environ["KB_DATABASE_URL"]
LITELLM = os.environ["LITELLM_BASE_URL"].rstrip("/")
MASTER = os.environ["LITELLM_MASTER_KEY"]
OPA = os.environ["OPA_URL"].rstrip("/")
OPA_HEADERS = {"Authorization": f"Bearer {os.environ.get('OPA_TOKEN_SEED', '')}"}  # system.authz: seed may PUT /v1/data/registry
REGISTRY_PATH = pathlib.Path(os.environ.get("REGISTRY_PATH", "/policy-data/registry.json"))
DOCS_DIR = pathlib.Path(__file__).parent / "docs"
EMBED_MODEL = "text-embedding-3-small"


def _pg(url):
    return psycopg.connect(url, autocommit=True)


def schema():
    with tracer.start_as_current_span("seed.schema"):
        with _pg(RAISIN_DB) as c:
            c.execute(pathlib.Path("schema.sql").read_text())
            for t in F.TENANTS:
                c.execute("INSERT INTO tenants VALUES (%s,%s) ON CONFLICT (id) DO NOTHING", t)
            for u in F.USERS:
                c.execute("INSERT INTO users VALUES (%s,%s,%s) ON CONFLICT (email) DO UPDATE SET tenant_id=EXCLUDED.tenant_id, role=EXCLUDED.role", u)
            for d in F.DONORS:
                c.execute("INSERT INTO donors (tenant_id,email,phone,name) VALUES (%s,%s,%s,%s) ON CONFLICT (tenant_id,email) DO NOTHING", d)
            for (did, tenant, email, amount, cur, status) in F.DONATIONS:
                donor_id = c.execute("SELECT id FROM donors WHERE tenant_id=%s AND email=%s", (tenant, email)).fetchone()[0]
                c.execute("INSERT INTO donations VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, donor_id=EXCLUDED.donor_id, amount=EXCLUDED.amount",
                          (did, tenant, donor_id, amount, cur, status))
            for tx in F.TRANSACTIONS:
                c.execute("INSERT INTO transactions VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET result=EXCLUDED.result, decline_code=EXCLUDED.decline_code", tx)
            for r in F.REGISTRY:
                c.execute("INSERT INTO ai_registry (kind,name,owner,risk_tier,approved) VALUES (%s,%s,%s,%s,%s) "
                          "ON CONFLICT (kind,name) DO UPDATE SET owner=EXCLUDED.owner, risk_tier=EXCLUDED.risk_tier", r)
        with _pg(KB_DB) as c:
            c.execute(pathlib.Path("kb_schema.sql").read_text())
        print("seed.schema ok")


def keys():
    with tracer.start_as_current_span("seed.keys") as span:
        created = []
        with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {MASTER}"}) as c:
            existing = c.get(f"{LITELLM}/key/list", params={"return_full_object": "true", "page": 1, "size": 100})
            aliases = set()
            if existing.status_code == 200:
                for k in existing.json().get("keys", []):
                    if isinstance(k, dict):
                        aliases.add(k.get("key_alias"))
            for alias, (env, budget, duration) in F.KEYS.items():
                if alias in aliases:
                    continue
                body = {"key": os.environ[env], "key_alias": alias, "max_budget": budget, "budget_duration": duration,
                        "models": F.KEY_MODELS.get(alias, ["gpt-4o-mini", "claude-sonnet", "text-embedding-3-small"]),
                        "metadata": {"tenant": alias.split("-")[0] if alias.startswith("tenant") else "platform", "poc": "aicp"}}
                r = c.post(f"{LITELLM}/key/generate", json=body)
                if r.status_code >= 400:
                    print(f"seed.keys {alias}: {r.status_code} {r.text[:200]}", file=sys.stderr)
                    r.raise_for_status()
                created.append(alias)
        span.set_attribute("keys.created", created)
        print(f"seed.keys ok created={created}")


def _embed(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    r = client.post(f"{LITELLM}/embeddings", json={"model": EMBED_MODEL, "input": texts})
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def index():
    with tracer.start_as_current_span("seed.index") as span:
        key = os.environ["LITELLM_KEY_SEED_INDEXER"]
        with _pg(KB_DB) as c, httpx.Client(timeout=60, headers={"Authorization": f"Bearer {key}"}) as http:
            n = 0
            for rel, (tenant, cls) in F.DOCS.items():
                text = (DOCS_DIR / rel).read_text().strip()
                # One chunk per doc: docs are short by design, and the poisoned doc must keep the procedure
                # text and the injection paragraph in the same chunk so retrieval ranks it with the real one.
                vec = _embed(http, [text])[0]
                c.execute("INSERT INTO chunks (doc_id, chunk_id, tenant_id, classification, content, embedding) VALUES (%s,0,%s,%s,%s,%s) "
                          "ON CONFLICT (doc_id, chunk_id) DO UPDATE SET content=EXCLUDED.content, embedding=EXCLUDED.embedding, "
                          "tenant_id=EXCLUDED.tenant_id, classification=EXCLUDED.classification",
                          (rel, tenant, cls, text, json.dumps(vec)))
                n += 1
        span.set_attribute("kb.docs", n)
        print(f"seed.index ok docs={n}")


def registry():
    with tracer.start_as_current_span("seed.registry") as span:
        with _pg(RAISIN_DB) as c:
            rows = c.execute("SELECT kind, name, owner, risk_tier, approved, version FROM ai_registry ORDER BY kind, name").fetchall()
        reg = {"models": {}, "tools": {}, "prompts": {}}
        for kind, name, owner, tier, approved, version in rows:
            reg[kind + "s"][name] = {"owner": owner, "risk_tier": tier, "approved": approved, "version": version}
        reg["revision"] = hashlib.sha256(json.dumps(reg, sort_keys=True).encode()).hexdigest()[:16]
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(json.dumps({"registry": reg}, indent=2))
        with httpx.Client(timeout=10, headers=OPA_HEADERS) as http:
            r = http.put(f"{OPA}/v1/data/registry", json=reg)
            r.raise_for_status()
        span.set_attribute("policy.registry_revision", reg["revision"])
        print(f"seed.registry ok revision={reg['revision']} file={REGISTRY_PATH}")


def flip(name: str, approved: str):
    val = approved.lower() in ("true", "1", "yes")
    with _pg(RAISIN_DB) as c:
        n = c.execute("UPDATE ai_registry SET approved=%s WHERE name=%s", (val, name)).rowcount
    print(f"seed.flip {name} approved={val} rows={n}")
    registry()


def reset():
    with _pg(RAISIN_DB) as c:
        for kind, name, owner, tier, approved in F.REGISTRY:
            c.execute("UPDATE ai_registry SET approved=%s WHERE kind=%s AND name=%s", (approved, kind, name))
    print("seed.reset ok")
    registry()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    t0 = time.time()
    if cmd == "all":
        schema(); keys(); registry()
        try:
            index()
        except Exception as e:  # noqa: BLE001
            # Embeddings need a working OPENAI_API_KEY behind LiteLLM. Everything else is seeded; re-run
            # `python seed.py index` once the key is in .env.
            print(f"seed.index FAILED (non-fatal): {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
    elif cmd == "schema":
        schema()
    elif cmd == "keys":
        keys()
    elif cmd == "index":
        index()
    elif cmd == "registry":
        registry()
    elif cmd == "flip":
        flip(sys.argv[2], sys.argv[3])
    elif cmd == "reset":
        reset()
    else:
        sys.exit(f"unknown command {cmd}")
    flush()
    print(f"seed done in {time.time()-t0:.1f}s")
