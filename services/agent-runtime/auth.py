"""JWT verification against raisin-api's JWKS. Claims (tenant_id, role) come from the token, never from
the prompt."""
import jwt
from fastapi import HTTPException
from jwt import PyJWKClient
from opentelemetry import trace

import config

# cache_keys=False: PyJWKClient's per-kid key cache never expires, so a rotated raisin-api key with a reused kid
# would fail forever. The JWK set itself is still cached (lifespan) and refetched when a kid is unknown.
_jwks = PyJWKClient(f"{config.RAISIN_API_URL}/.well-known/jwks.json", cache_keys=False, cache_jwk_set=True, lifespan=60)


def verify_bearer(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, {"error": "missing bearer token"})
    token = authorization.split(" ", 1)[1]
    try:
        try:
            key = _jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience="aicp", issuer="raisin-api")
        except jwt.InvalidSignatureError:
            # key rotated under the same kid: refresh the JWKS once and retry
            _jwks.get_jwk_set(refresh=True)
            key = _jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience="aicp", issuer="raisin-api")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(401, {"error": f"invalid token: {type(e).__name__}"}) from e
    span = trace.get_current_span()
    span.set_attribute("enduser.id", claims["sub"])
    span.set_attribute("tenant_id", claims["tenant_id"])
    span.set_attribute("role", claims["role"])
    return claims
