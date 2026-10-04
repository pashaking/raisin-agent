"""RS256 signing key for the built-in identity route. Dev-only: a fresh keypair is generated at
boot unless JWT_PRIVATE_KEY_PEM is provided. Production analogue is Entra ID; Dex is the local
OIDC swap named in the design."""
import base64
import hashlib
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# kid is the RFC 7638-style thumbprint of the public key, so a restart with a fresh dev key rotates the kid
# and verifiers (agent-runtime) refetch the JWKS instead of trusting a stale cached key.
KID_PREFIX = "aicp-"


def _b64u(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def load_or_generate():
    pem = os.environ.get("JWT_PRIVATE_KEY_PEM", "").strip()
    if pem:
        priv = serialization.load_pem_private_key(pem.encode(), password=None)
    else:
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = priv.public_key()
    pub_pem = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    nums = pub.public_numbers()
    kid = KID_PREFIX + hashlib.sha256(pub_pem.encode()).hexdigest()[:16]
    jwks = {"keys": [{"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": _b64u(nums.n), "e": _b64u(nums.e)}]}
    return priv_pem, pub_pem, jwks, kid
