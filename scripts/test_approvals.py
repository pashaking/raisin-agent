#!/usr/bin/env python3
"""Approval workflow assertions against the running stack (Phase 10): deterministic, no LLM call.

Pending approval rows are normally created only by the model requesting resend_receipt through /assistant (OPA
tool-stage requires_approval). That path is exercised manually/via the console; this script seeds rows directly
(`docker compose exec agent-runtime python -c "import approvals; ..."`) -- the same technique `make opa-query` /
`make opa-data` already use to reach into the running container -- so the HTTP contract and the security properties
below can be asserted fast and without depending on model phrasing or guardrail judge variance:

  - tenant isolation: another tenant's Finance cannot see or decide a pending request (404, not a leaking 403)
  - role gate: only Finance may list or decide (403 for Donor/Participant)
  - approval integrity: decide takes no tool/args, only a decision on an id; the exact stored args run unchanged
  - state machine: reject is terminal; approve executes and is terminal; deciding twice is 409, not a silent no-op
  - the requester may always check their own request's status, even without the Finance role

usage: test_approvals.py
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import demo  # noqa: E402

RUNTIME = "http://localhost:18090"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, info: str = ""):
    RESULTS.append((name, bool(cond), info))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{info}]" if info and not cond else ""))


def call(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{RUNTIME}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {}


def seed_pending(tenant_id: str, requested_by: str, donation_id: int) -> str:
    """Create a pending approval row directly inside the agent-runtime container, bypassing the model."""
    snippet = (
        "import approvals, json;"
        f"print(approvals.create({tenant_id!r}, {requested_by!r}, None, 'resend_receipt', {{'donation_id': {donation_id}}}, 'high'))"
    )
    out = subprocess.run(["docker", "compose", "exec", "-T", "agent-runtime", "python", "-c", snippet],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


FIN_A, FIN_B, DONOR_B = "finance.a@charity-a.test", "finance.b@charity-b.test", "donor.b@example.test"


def main():
    tok_a = demo.token(FIN_A)
    tok_b = demo.token(FIN_B)
    tok_donor_b = demo.token(DONOR_B)

    appr_reject = seed_pending("tenant-a", FIN_A, 873902)
    appr_approve = seed_pending("tenant-a", FIN_A, 873903)
    check("seeded two pending approvals", bool(appr_reject) and bool(appr_approve) and appr_reject != appr_approve,
          f"{appr_reject!r} {appr_approve!r}")

    st, b = call("GET", "/approvals", token=tok_a)
    ids = {r["id"] for r in b.get("pending", [])}
    check("list (Finance A): sees both seeded pending rows", st == 200 and {appr_reject, appr_approve} <= ids, json.dumps(b)[:300])

    st, b = call("GET", "/approvals", token=tok_b)
    check("list (Finance B): tenant isolation -- sees none of tenant-a's pending rows", st == 200 and b.get("pending") == [], json.dumps(b)[:300])

    st, b = call("GET", "/approvals", token=tok_donor_b)
    check("list (Donor): role gate -- 403, not Finance", st == 403, json.dumps(b)[:200])

    st, b = call("POST", f"/approvals/{appr_reject}/decide", {"decision": "approve"}, token=tok_b)
    check("decide (Finance B on tenant-a's row): tenant isolation -- 404, not a leaking 403", st == 404, json.dumps(b)[:200])

    st, b = call("POST", f"/approvals/{appr_reject}/decide", {"decision": "reject"}, token=tok_a)
    check("decide reject (Finance A, own tenant): 200, status rejected", st == 200 and b.get("status") == "rejected", json.dumps(b)[:200])

    st, b = call("POST", f"/approvals/{appr_reject}/decide", {"decision": "approve"}, token=tok_a)
    check("decide again on an already-rejected row: 409, not silently re-decided", st == 409, json.dumps(b)[:200])

    st, b = call("POST", f"/approvals/{appr_approve}/decide", {"decision": "approve"}, token=tok_a)
    check("decide approve (Finance A, own tenant): 200, status executed, result carries the real backend write",
          st == 200 and b.get("status") == "executed" and b.get("result", {}).get("donation_id") == 873903 and "receipt_resent_at" in b.get("result", {}),
          json.dumps(b)[:300])

    st, b = call("GET", f"/approvals/{appr_approve}", token=tok_a)
    check("GET single (approver, Finance): full row with decided_by/result", st == 200 and b.get("status") == "executed" and b.get("decided_by") == FIN_A, json.dumps(b)[:300])

    appr_self_check = seed_pending("tenant-b", DONOR_B, 991202)
    st, b = call("GET", f"/approvals/{appr_self_check}", token=tok_donor_b)
    check("GET single (the original requester, not Finance): can still check their own request", st == 200 and b.get("id") == appr_self_check, json.dumps(b)[:200])
    call("POST", f"/approvals/{appr_self_check}/decide", {"decision": "reject"}, token=demo.token(FIN_B))  # clean up

    st, b = call("GET", "/approvals/APR-does-not-exist", token=tok_a)
    check("GET nonexistent id: 404, not a 500", st == 404, json.dumps(b)[:200])


if __name__ == "__main__":
    main()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nAPPROVAL TESTS: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed ->", "PASS" if not failed else "FAIL")
    sys.exit(0 if not failed else 1)
