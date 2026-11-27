# Cloudflare Email Sending as a built-in email transport

Date: 2026-11-27. Refines
[2026-10-19 distribution delivery boundary](2026-10-19-distribution-delivery-boundary.zh.md),
which made SMTP the core's only built-in email transport and every vendor
transport an opt-in `BACKEND_EXTENSIONS` module.

## Decision

The gateway ships two built-in email transports, `smtp` and `cloudflare`
(Cloudflare Email Sending's REST API), and the first-run setup step asks the
administrator to choose one. Neither is configured or assumed by default:
`EMAIL_PROVIDER` still defaults to `smtp`, and since the change that removed
the Resend defaults, SMTP has no server or account either.

Why built in rather than opt-in: the choice has to be available in the
first-run setup, before anyone can edit the deployment's environment, and an
opt-in module is selected only through `BACKEND_EXTENSIONS`, an
environment-only setting. Cloudflare needs two settings and a single JSON
request, with no templates to approve or region policy. Those are what kept
Tencent SES out of the core. FreeInference ran the same transport as a
distribution module that rebound the gateway's mail functions; it moves here.

Unchanged: Tencent SES and SMS stay an opt-in module. A selected transport
never falls back to another. Extensions register further transports through
`register_email_provider`, and cannot take the name `cloudflare`.

## Contract

- Settings: `CLOUDFLARE_ACCOUNT_ID` (32 hexadecimal digits, security-relevant)
  and `CLOUDFLARE_EMAIL_API_TOKEN` (secret). Both are read on every send. They
  are required only while `EMAIL_PROVIDER=cloudflare` and public signup
  requires email verification. The names are the ones FreeInference's module
  registered, so its stored values carry over.
- Sender: `SMTP_FROM_EMAIL` and `SMTP_FROM_NAME` are the sender of both built-in
  transports.
- Success means Cloudflare reported the message delivered or queued. Refusals,
  bounces and suppressed recipients are logged with Cloudflare's error code
  only, never text that could echo the request. No automatic retry.
- `ConfigEntry.applies_when` (`AppliesWhen(key, values)`) declares that a
  setting applies only while another holds one of the listed values. The admin
  API reports it with each entry's `choices`. The console shows a choice as a
  select and hides the settings of the transports not selected, in the setup
  step and in Admin Settings; their unsaved edits are neither counted nor sent.
  The Tencent SES settings declare it as well. Requirements stay their own
  `required` rule.

## Migration

On a pin with this transport, FreeInference's module as it stands aborts
startup: its `register_config_entries` call names what are now core entries,
which an extension cannot replace, and a failed registration is fatal. So the
move is staged, with mail flowing throughout:

1. Before the bump, the module registers its two settings only when the
   gateway does not list them already, and keeps sending as before.
2. After the bump, an administrator stores `EMAIL_PROVIDER=cloudflare` (only
   that pin accepts the value). The module still carries the mail.
3. FreeInference deletes `distributions/freeinference/cloudflare_email.py`, and
   the built-in transport sends with the same stored settings. Its fallback to
   SMTP ends with it: a selected transport does not fall back.

A host that already exports `CLOUDFLARE_ACCOUNT_ID` for other tools (Wrangler,
R2) has it imported as this setting on first start, as with any registered
setting. It does nothing unless `EMAIL_PROVIDER=cloudflare`.
