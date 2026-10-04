# payment-gateway

A stub with one job: exist as a separate `service.name` in traces so the "payment path never touches AI" claim can be
asserted rather than asserted-in-prose. Design box: **Payment gateway (Stripe / Moneris / Adyen)**.

| | |
|---|---|
| Build | `services/payment-gateway/Dockerfile` (FastAPI) |
| Port | 18070 → 8070 |
| Depends on | `otel-collector` |
| Health | `GET /health` |
| Code | `services/payment-gateway/app.py` (40 lines) |

## Interface

`POST /charge {amount, currency, payment_token}` → `{result, decline_code, gateway_ref}`.
Tokens starting with `tok_decline` decline with issuer code `51` (insufficient funds); anything else approves.
The payload is already minimized by `raisin-api`: no donor fields ever reach this service.

## Spans

`POST /charge` with `payment.result`, `payment.amount`, `payment.currency`, `payment.decline_code`. Never a `gen_ai.*`
attribute. `scripts/test_traces.py` asserts: at least one `raisin-api`-rooted trace touches this service, at least one has
`gen_ai.*`, none has both.

## When it is down

`/donate` returns 500 (`raisin-api` calls `raise_for_status()` on the charge). The AI path is unaffected, which is itself
part of the point: the two paths share `raisin-api` and `postgres` but nothing else.

## Success example

Scenario 0, trace `065e3c6954bc51f83a4e2d16c44a7f43`:

```
raisin-api       POST /donate      tenant_id=tenant-a  donation.id=900015  payment.result=declined
 payment-gateway  POST /charge    12 ms  payment.result=declined  payment.decline_code=51
```

Response to the client: `{"donation_id": 900015, "result": "declined", "decline_code": "51", "trace_id": "065e3c..."}`.
The canary donor's email and phone were in the request body and are in zero span attributes anywhere (`make test`, PII canary).

## Failure example

There is no failure mode inside the stub itself beyond being unreachable. The failure it guards against is architectural:
if a future change made `raisin-api` call the runtime from inside `/donate`, the PCI-scope assertion in `make test` would
fail on the first `make demo` because one trace would contain both `payment-gateway` and `gen_ai.*`.

## Related

- [raisin-api.md](raisin-api.md) (`/donate`), [../05-observability-reference.md](../05-observability-reference.md#assertions-that-run-against-traces-make-test)
