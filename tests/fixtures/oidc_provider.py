"""An OpenID Connect identity provider that lives in the test process.

``FakeOidcProvider.handler`` is an ``httpx.MockTransport`` handler: it serves a
discovery document, the provider's keys, a token endpoint and UserInfo. ID
tokens are signed with a real RSA key, so the client under test verifies real
signatures. Install it with ``monkeypatch.setattr(oidc, "_transport",
httpx.MockTransport(provider.handler))``.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.example.edu/realms/main"
CLIENT_ID = "hybridinference"


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


#: The key the provider publishes, and one it does not.
PRIMARY_KEY = _rsa_key()
OTHER_KEY = _rsa_key()


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    entry = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**entry, "kid": kid, "use": "sig", "alg": "RS256"}


def id_token_claims(nonce: str | None, **overrides: Any) -> dict[str, Any]:
    """Claims of a valid ID token; an override of None removes the claim."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": "subject-1",
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
        "email": "Alice@Example.EDU",
        "email_verified": True,
        "name": "Alice Example",
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def sign_id_token(claims: dict[str, Any], *, key=PRIMARY_KEY, kid: str | None = "key-1") -> str:
    """Sign *claims* as the provider would."""
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid} if kid else None)


class FakeOidcProvider:
    """A scriptable identity provider behind ``httpx.MockTransport``."""

    def __init__(self) -> None:
        self.issuer = ISSUER
        self.keys = {"key-1": PRIMARY_KEY}
        self.requests: list[httpx.Request] = []
        self.discovery_overrides: dict[str, Any] = {}
        self.token_status = 200
        self.token_response: dict[str, Any] | None = None
        self.userinfo: dict[str, Any] | None = None
        self.unreachable = False
        self.id_token = ""

    def discovery(self) -> dict[str, Any]:
        document = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/keys",
            "userinfo_endpoint": f"{ISSUER}/userinfo",
        }
        return {**document, **self.discovery_overrides}

    def count(self, suffix: str) -> int:
        """How many requests reached the endpoint whose path ends with *suffix*."""
        return sum(1 for request in self.requests if request.url.path.endswith(suffix))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=self.discovery())
        if path.endswith("/keys"):
            return httpx.Response(
                200, json={"keys": [_jwk(key, kid) for kid, key in self.keys.items()]}
            )
        if path.endswith("/token"):
            body = self.token_response
            if body is None:
                body = {"id_token": self.id_token, "access_token": "at-1", "token_type": "Bearer"}
            return httpx.Response(self.token_status, json=body)
        if path.endswith("/userinfo"):
            if self.userinfo is None:
                return httpx.Response(404)
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(404)
