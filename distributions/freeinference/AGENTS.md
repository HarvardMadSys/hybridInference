# Working on the FreeInference deployment

Read [/AGENTS.md](../../AGENTS.md) first — it covers the project. This file
covers the one deployment, and lives here rather than at the repository root
because the root ships with the source: a host or a credential written there is
published to everyone who clones it.

## Hosts

- **Production:** https://freeinference.org
- **Staging:** https://staging.freeinference.org (deployed from `dev`)
- **Public docs:** https://doc.freeinference.org/
- **Internal docs:** `internaldoc.freeinference.org` (Harvard SEAS network only —
  deliberately not a link, since it resolves for nobody outside)

## Verifying a change

Staging deploys from `dev`, so a merged PR is on staging within a few minutes.
Verify there before calling a change done.

The staging test account is in the team password manager, not in this file.
Ask if you do not have it.

> It used to be spelled out in the root `AGENTS.md`, `CLAUDE.md` and the
> `.codex`/`.kilo` skill guides — removed here — and it still appears in
> archived plan documents under `docs/agents/plans/`.
>
> Those matter. The current repository itself will become public; there is no
> filtered export hiding its tree or history. Rotate the password, then remove
> the private value from every retained ref during the coordinated history
> cleanup described by the
> [direct-publication readiness plan](../../docs/agents/plans/2026-08-26-direct-publication-readiness.md).

## Operational tooling

`ops/` holds this deployment's scripts — deploy, database, Cloudflare, on-call.
`services/` holds its workers. Site-specific pieces move to the private
FreeInference repository before HybridInference becomes public; neutral
upstream tooling stays here.
