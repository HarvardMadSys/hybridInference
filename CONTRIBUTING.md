# Contributing

The contribution guide lives with the rest of the developer documentation:
**[doc.hybridinference.org/contributing.html](https://doc.hybridinference.org/contributing.html)**,
sources in [`docs/developer/contributing.md`](docs/developer/contributing.md).
Edit the source, not a copy.

New here? Start with the guide's
[first contribution walkthrough](docs/developer/contributing.md#your-first-contribution):
create a worktree, add a small routing-config test, verify a running gateway,
and open a pull request. For release identity, support expectations and upgrade
planning, see [Releases and upgrades](docs/developer/releases.md).

It covers getting set up, what the repository contains, the quality gates a
change has to pass, the four test tiers, and how a change gets proposed. Three
things are worth knowing before you open anything:

- **Branch off `dev`, not `main`**, and target `dev` with the pull request.
- **Run `make format` and `make test` first.** CI additionally runs the `dbtest`
  tier that `make test` excludes, so a storage or auth change can be green
  locally and red in CI; `make test-db` against a local Postgres covers that.
- **Titles are conventional commits** — `type(scope): summary`. History on `dev`
  is squashed to one commit per pull request, so the title you write becomes the
  commit subject.

To report a security vulnerability, follow [SECURITY.md](SECURITY.md) rather
than opening an issue.
