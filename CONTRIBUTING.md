# Contributing

The contribution guide is part of the developer documentation:
**[doc.hybridinference.org/contributing.html](https://doc.hybridinference.org/contributing.html)**,
with its source in [`docs/developer/contributing.md`](docs/developer/contributing.md)
(edit the source, not a copy). New contributors can start with its
[first contribution walkthrough](docs/developer/contributing.md#your-first-contribution).
For release identity, support and upgrades, see
[Releases and upgrades](docs/developer/releases.md).

Before you open a pull request:

- Branch off `dev`, not `main`, and target `dev`.
- Run `make format` and `make test`. CI also runs the `dbtest` tier, which
  `make test` skips, so a storage or auth change can pass locally and fail in
  CI; run that tier with `make test-db-local` (Docker) or `make test-db`
  against your own PostgreSQL.
- Write the title as a conventional commit, `type(scope): summary`. `dev` is
  squash-merged, so the title becomes the commit subject.

Report security vulnerabilities as [SECURITY.md](SECURITY.md) describes, not in
an issue.
