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

## What this software leaves to you

Two things are the operator's responsibility by design, and neither is a
vulnerability in the project:

- **Credentials.** The repository ships none. Provider keys, `JWT_SECRET_KEY`
  and `API_KEY_SECRET` all come from your environment, and the gateway logs a
  `critical` line and keeps running with insecure defaults if the last two are
  unset. See [Installation](docs/developer/installation.md).
- **Exposure.** Defaults bind to loopback; publishing the console or the API to
  a network, and terminating TLS in front of it, is a deployment decision. The
  client-address rules that rate limiting and abuse blocking depend on are in
  [Trusted Proxies and Client IPs](docs/developer/trusted-proxies-and-client-ips.md),
  which is worth reading before you put one behind a proxy.
