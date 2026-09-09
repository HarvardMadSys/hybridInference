# Security Policy

## Reporting a vulnerability

Report privately, not in a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/HarvardMadSys/hybridInference/security/advisories/new)
for this repository, which opens a draft advisory only the maintainers can see.

Include what you would need yourself: the affected version or commit, how the
gateway was configured, the steps that reproduce it, and what an attacker gets.
A proof of concept against your own instance helps; please do not test against
someone else's.

Expect an acknowledgement within a few working days. This is a research group's
project, not a vendor with an on-call rotation, so treat that as a good-faith
target rather than a guarantee.

## Scope

In scope: the gateway backend and console in this repository, its default
configuration, and the deployment artifacts under `deploy/`.

Out of scope: the upstream model providers this gateway routes to — report
those to the provider — and any particular installation's operational setup,
including [FreeInference](https://freeinference.org/), which is one deployment
of this software rather than the software itself.

## Deployment configuration

- **Credentials.** Supply your own provider keys, `JWT_SECRET_KEY` and
  `API_KEY_SECRET`. Empty or whitespace-only authentication secrets stop the
  gateway before it opens stores or starts background tasks whenever the
  database or user authentication is enabled. Only a gateway with both
  `DB_ENABLED=false` and `USER_AUTH_ENABLED=false` can start without them.
  Disabling inference authentication alone does not disable database-backed
  login or API-key management. Keep existing secrets across upgrades; see
  [Installation](docs/developer/installation.md) for setup and rotation effects.
  A blank `ADMIN_TOKEN` disables only the optional legacy admin-token path.
- **Exposure.** The supplied Compose stack publishes the frontend, backend and
  database on loopback by default. `FRONTEND_HOST` and `BACKEND_HOST` can
  explicitly expose the application ports on another interface. The console
  forwards API routes, so exposing it also exposes those routes. Configure TLS
  and network access for the deployment, and follow
  [Trusted Proxies and Client IPs](docs/developer/trusted-proxies-and-client-ips.md)
  before trusting forwarded client addresses for rate limiting and abuse blocking.

The runnable example contains conspicuous local-only credentials for its
authenticated demo. Replace those when creating a deployment of your own.
Release support and upgrade expectations are documented in
[Releases and upgrades](docs/developer/releases.md).
