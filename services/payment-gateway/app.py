"""Payment gateway stub. Exists so `service.name=payment-gateway` appears in traces and the
PCI-scope assertion (no gen_ai.* span in any trace that touches this service) is meaningful.
Payload is already minimized by raisin-api: {amount, currency, payment_token}. No donor fields.
"""
from fastapi import FastAPI
from opentelemetry import trace
from pydantic import BaseModel

from otel_setup import init_tracing, instrument_fastapi

tracer = init_tracing("payment-gateway")
app = FastAPI(title="payment-gateway")
instrument_fastapi(app)


class Charge(BaseModel):
    amount: float
    currency: str
    payment_token: str


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/charge")
def charge(c: Charge):
    span = trace.get_current_span()
    # tok_decline_* tokens decline with issuer code 51 (insufficient funds); anything else approves.
    if c.payment_token.startswith("tok_decline"):
        result, code = "declined", "51"
    else:
        result, code = "approved", None
    span.set_attribute("payment.result", result)
    span.set_attribute("payment.amount", c.amount)
    span.set_attribute("payment.currency", c.currency)
    if code:
        span.set_attribute("payment.decline_code", code)
    return {"result": result, "decline_code": code, "gateway_ref": f"pg_{abs(hash(c.payment_token)) % 10**8}"}
