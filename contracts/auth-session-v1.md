# Auth/session contract v1

Browser clients are expected to reach the frontend and backend through the
same origin. Cross-origin cookie or CORS relaxation is not part of this
contract.

The stable behavior is:

- `POST /auth/login` accepts credentials, returns a short-lived bearer access
  token, and sets a `refresh_token` cookie with `HttpOnly`, `Secure`, root path,
  and `SameSite=Lax`.
- `POST /auth/refresh` authenticates with that cookie, rotates it, invalidates
  the prior stored token, and returns a new bearer access token.
- `GET /user/me` authenticates with the bearer token. Account status and role
  come from current backend state, not stale JWT or frontend state.
- Permission checks are enforced by the backend. Capability discovery may
  control rendering, but never grants permission.

The dependency-free gate is
`tests/servers/test_contract_auth_session.py`. The PostgreSQL-backed
characterization suite in `tests/servers/test_auth_routes.py` additionally
covers login rejection status, logout/revocation, replay of a rotated refresh
token, email-verification status, role bootstrap, and the complete
login-refresh-me flow. `tests/servers/test_internal.py` covers the operator
session role boundary.
