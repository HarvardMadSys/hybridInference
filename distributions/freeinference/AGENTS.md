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
> Those matter. The publication route in the
> [split design](../../docs/agents/specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)
> is a filtered export into a new public repository, and rewriting this repo's
> history is a stated hard constraint — so this repository's history is never
> published, and what the export carries is the tree, not the past. Cleaning
> the tree is the effective action, and the public-surface audit on that
> checklist is where it belongs. **Rotate the password anyway**, as cheap
> insurance and because it has been readable to everyone with repository
> access for months.

## Operational tooling

`ops/` holds this deployment's scripts — deploy, database, Cloudflare, on-call.
`services/` holds its workers. Treat both as this deployment's rather than the
project's: where they end up is settled by the split design's visibility rules,
not by this file.
