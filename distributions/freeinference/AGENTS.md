# Working on the FreeInference deployment

Read [/AGENTS.md](../../AGENTS.md) first — it covers the project. This file
covers the one deployment, and lives here rather than at the repository root
because the root ships with the source: a host or a credential written there is
published to everyone who clones it.

## Hosts

- **Production:** https://freeinference.org
- **Staging:** https://staging.freeinference.org (deployed from `dev`)
- **Public docs:** https://doc.freeinference.org/
- **Internal docs:** internaldoc.freeinference.org (Harvard SEAS network only)

## Verifying a change

Staging deploys from `dev`, so a merged PR is on staging within a few minutes.
Verify there before calling a change done.

The staging test account is in the team password manager, not in this file.
Ask if you do not have it.

> It used to be spelled out in the root `AGENTS.md`, `CLAUDE.md` and the
> `.codex`/`.kilo` skill guides — removed here — and it still appears in
> archived plan documents under `docs/agents/plans/`. Scrubbing those is not
> the fix: anything ever committed stays in the history, which goes public with
> the repository. **Rotate it.**

## Operational tooling

`ops/` holds this deployment's scripts — deploy, database, Cloudflare, on-call.
`services/` holds its workers. Both move to the FreeInference repository in
step 2 of the split; until then, treat them as this deployment's, not the
project's.
