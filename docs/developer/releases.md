# Releases and upgrades

Run an installation you operate from a published release; contributions target
`dev`. This page covers telling what is running and planning an upgrade. Setup
is in [Installation](installation.md) and the service commands in
[Deployment](deployment.md).

## Release identity and support

Releases are [GitHub releases](https://github.com/HarvardMadSys/hybridInference/releases)
tagged with the UTC date, `YYYYMMDD`, plus `.1`, `.2` and so on for further
releases that day. They are dated snapshots, not semantic-version promises. A
maintainer cuts one by moving `main` up to a commit on `dev`, and the `Release`
workflow publishes it with generated notes.

| Identifier | What it tells you |
|---|---|
| A published date tag | A fixed source snapshot with GitHub release notes |
| `main` | The branch maintainers move up to `dev` for each release; it moves over time |
| `dev` or a feature branch | Development work that may be newer than the latest release |
| A full commit SHA | The exact source revision; report local modifications separately |
| An image digest, `image@sha256:…` | The exact container artifact, which may differ from the host checkout |

Container images are published only for releases: when `main` moves, CI builds
the backend and console images and, once that run is green, publishes them
tagged `sha-<full-sha>`, with short-SHA aliases. An unreleased `dev` commit has
no published image; build it from source. Images published earlier carry
`dev-<full-sha>` tags. The `0.1.0` in `pyproject.toml` and
`apps/frontend/package.json` identifies no build.

Report problems against the latest release when you can. Fixes land on `dev` and
reach a later release; there is no LTS branch, backport window or response-time
commitment. For an older version, include its exact revision and whether the
latest release has the problem too. Report security issues as
[the security policy](https://github.com/HarvardMadSys/hybridInference/blob/dev/SECURITY.md)
describes.

### Include the version in a bug report

In the checkout used to build or start the service:

```bash
git rev-parse HEAD
git describe --tags --exact-match HEAD
git status --short
```

The second command fails when HEAD is not exactly a tag; report the SHA then.
Mention local changes without pasting secrets or private configuration.

For the Compose stack, report the running images:

```bash
docker inspect --format '{{.Name}} image={{.Config.Image}} id={{.Image}}' \
  hybridinference-backend hybridinference-frontend
```

Use your container names if they differ, and include any pinned image digests.
The console footer shows the console's build SHA when one was supplied at build
time (`build dev` or `local build` means none was); report the backend revision
separately.

## Compatibility and breaking changes

Read the notes and linked PRs of every release between your version and the
target. A date tag does not establish compatibility, and OpenAI compatibility
does not cover every provider extension; check the endpoints, streaming and
model settings your clients use.

If you change an HTTP contract, authentication, a setting or configuration
document, a default or the database schema, state the old and new behavior in
the PR: who must act, the configuration or migration steps, and whether the
previous version can still use the resulting data. Mark breaking changes in the
PR and the release notes; generated commit summaries do not tell an operator how
to upgrade.

## Upgrade and rollback

1. Record the current and target revisions, the backend and console image
   identities, and the active distribution. Keep the previous images so that a
   rollback needs no rebuild.
2. Read the target's configuration requirements. Merge changes into the
   deployment's existing `.env`, overlay and secrets instead of replacing them
   with example files. An upgrade never rotates secrets: changing
   `JWT_SECRET_KEY` signs everyone out, changing `API_KEY_SECRET` breaks every
   API key, and changing `ERASURE_FENCE_SECRET` (or `API_KEY_SECRET` while it
   stands in for it) stops the backend from starting. Keep the `.env` you
   upgraded from until rollback is no longer an option; see
   [Upgrading a deployment that kept its settings in `.env`](deployment.md#upgrading-a-deployment-that-kept-its-settings-in-env).
3. Back up each database and check that the backup restores; see
   [Backup](database.md#backup). The backup holds the stored secrets in
   plaintext, so protect it like them.
4. Rehearse on a separate local or staging deployment with its own storage.
   Apply the migrations with the new image
   ([How the schema is created](database.md#how-the-schema-is-created)), start
   it, and check the startup logs, `/health/ready`, sign-in, an existing API
   key, a routed request and streaming. Then start the previous image on the
   migrated database and run the same checks, since that is what a failed deploy
   falls back to.
5. Deploy with your usual method: migrate with the new image while the old one
   serves, and restart only once that succeeded. On a stack built from source,
   `make build` rebuilds, migrates and recreates the services; `make restart`
   does none of that (see
   [What a change actually requires](deployment.md#what-a-change-actually-requires)).
   Check the running image identities and repeat the rehearsal checks before
   sending normal traffic.

   `make deploy` deploys published images by digest instead, and restores the
   previous ones if acceptance fails; see
   [Diagnostics, deployment and recovery](deployment.md#diagnostics-deployment-and-recovery).

   Upgrading from a release that read its site identity, branding and alert
   rules from files, add `LEGACY_MANIFEST=<manifest>` to `make deploy` or
   `make build` so the new image imports them between the migrations and the
   restart. Until the import runs, the new release serves neutral site settings
   and the built-in alert thresholds. See
   [Model and routing documents in the database](configuration.md#model-and-routing-documents-in-the-database).

A migration leaves the previous release working on the new schema, so restoring
the previous images and configuration is the rollback; do not run the previous
image's migrations. A release whose migration drops something older code uses
says so in its notes, and older backends then refuse to start on that database.
Going back past such a release means restoring the pre-upgrade backup, which
loses later writes. There is no schema downgrade, and deleting a volume is a
reset, not a rollback.

`make rollback` returns to the images the last accepted `make deploy` replaced.
A release from before the database-backed configuration reads its files through
`DISTRIBUTION_CONFIG_PATH`, `DISTRIBUTION_CONFIG_MODE` and the `*_CONFIG_PATH`
variables, which Compose still passes to the backend. Keep them, and the files
they name, until a rollback to such a release is no longer an option.
