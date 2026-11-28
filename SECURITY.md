# Security Policy

## Reporting a vulnerability

Report privately through GitHub's
[private vulnerability reporting](https://github.com/HarvardMadSys/hybridInference/security/advisories/new),
not in a public issue; it opens a draft advisory only the maintainers can see.
Include the affected version or commit, how the gateway was configured, the
steps to reproduce, and what an attacker gains. A proof of concept against your
own instance helps; do not test against anyone else's.

Expect an acknowledgement within a few working days. This is a research group's
project, not a vendor with an on-call rotation, so treat that as a good-faith
target, not a guarantee.

## Scope

In scope: the gateway backend and console in this repository, its default
configuration, and the deployment artifacts under `deploy/`.

Out of scope: the upstream model providers (report to the provider) and any
installation's operational setup, including
[FreeInference](https://freeinference.org/), which is one deployment of this
software, not the software itself.

## Deployment configuration

### Secrets and the database

Supply your own provider keys. A gateway with a database generates
`JWT_SECRET_KEY` and `API_KEY_SECRET` into it on first start and stores every
setting there in plaintext, provider keys included. Anyone who can read the
database (a dump, a backup, pgAdmin) can use those keys, decrypt users' stored
API keys and sign administrator tokens, so protect database access and backups.

- The admin API shows whether a secret is set, never its value. The one
  exception is a configuration backup with secrets, which only an administrator
  signed in to the console can download, after confirming who they are. The
  default backup names each secret without its value.
- A backup file is not signed. Restoring one changes a secret, the security,
  network or sign-up settings, or where requests, mail and users are sent only
  after the administrator confirms each change; check what a file changes first.
- The same confirmation guards changing or resetting those settings on the
  Settings tab, raising an account's role, creating an API key for another
  account, and changing who may use a model. Depending on the account, the
  administrator confirms with an authenticator code, the password, or a new
  single sign-on login (see
  [Configuration](docs/developer/configuration.md#on-the-settings-tab)).
  `ADMIN_TOKEN` cannot confirm, so it can do none of these. On the host,
  `python -m serving.config.manage` can still change any setting.
- Verifying a provider route sends its stored keys only where they already go:
  the scheme, host and port of the provider's own base URL or of one its routes
  uses. Saving a route that would send them anywhere else, plain http or another
  port on the provider's host included, needs the same confirmation first, so
  `ADMIN_TOKEN` cannot do it. The audit log records every check, Verify's and a
  save's, a failed one too, and every refused confirmation.
- Without a database, secrets come from the environment. While user
  authentication is enabled, an empty or whitespace-only secret stops the
  gateway before it opens stores or starts background tasks; only a gateway
  with both `DB_ENABLED=false` and `USER_AUTH_ENABLED=false` starts without
  them. Disabling inference authentication alone does not disable
  database-backed login or API-key management.
- Keep existing secrets across upgrades;
  [Installation](docs/developer/installation.md) covers setup and what rotation
  affects. A blank `ADMIN_TOKEN` disables only the optional legacy admin-token
  path.

### First administrator

A new database-backed deployment refuses sign-ups until someone creates the
first administrator with the one-time setup code, which the backend keeps in
its database and prints in its log; treat both as privileged until setup is
done. Give that administrator a hard-to-guess username: sign-in limits count
attempts before checking the password, so anyone who knows a username can lock
it out of every browser that has not signed in to it before. A browser that has
keeps a device cookie with its own attempt count.

### Exposure

The supplied Compose stack publishes the frontend, backend and database on
loopback by default; `FRONTEND_HOST` and `BACKEND_HOST` expose the application
ports on another interface. The console forwards API routes, so exposing it
exposes them too. Configure TLS and network access, and follow
[Trusted Proxies and Client IPs](docs/developer/trusted-proxies-and-client-ips.md)
before trusting forwarded client addresses for rate limiting and abuse blocking.

The runnable example ships conspicuous local-only credentials for its demo;
replace them in a deployment of your own. Release support and upgrade
expectations are in [Releases and upgrades](docs/developer/releases.md).
