# Cloud agent sandbox — operations

What is on `dev` today, what is verified, and the exact steps left before a
job can run for real. This page is about operating it; design rationale lives
in the project's issue history.

## What a job does

```text
POST /v1/agent/jobs           owner queues work
  → dispatcher claims          internal-role credential, mints three tokens
    → runner checks out        read-only clone credential, never enters the sandbox
    → sandbox runs the agent   only a model-scoped token crosses the boundary
      → normalized events      streamed to the owner over SSE
      → model calls            billed through the gateway into api_logs
    → patch artifact           the agent never pushes
  → publisher validates        .github/ block, secret scan, path escape, size
    → agent/<job-id> branch    pinned refspec, never dev/main
      → draft PR               a human reviews before anything merges
```

Two properties are load-bearing and easy to break by accident:

- **No repository credential ever enters the sandbox.** The agent emits a
  patch; the publisher — running in the gateway, outside the sandbox — is the
  only component holding a GitHub token that can *write*. The runner does hold
  a clone credential, but it is read-only, scoped to the one repository, and
  deliberately never recorded in `.git/config`: the checkout adds no remote,
  the credential travels in git's environment rather than its argv, and the
  runner refuses to start the agent if it finds the token anywhere under
  `.git/`.
- **The sandbox's only credential is model-scoped.** It buys inference against
  the job's capped budget. It cannot write the event log, overwrite the patch,
  or move the job to a terminal state; the runner holds the token that can.

## Verified

Against real components on a developer machine:

| | How |
|---|---|
| Store: fencing, reaper, one-shot publish | dbtests against Postgres |
| Job API, SSE resume, worker endpoints | live gateway |
| Per-job credential: auto-revoke, budget 429 | live gateway |
| Runner + runtime adapter | real Claude Code CLI, end to end |
| Publish: patch → branch → draft PR | real `git push` to a local bare repo |
| **A real job on a real model** | live Claude Code CLI × `deepseek-v4-flash` through the production gateway: docstring landed on disk, patch produced, 3 calls / 74,523 tokens / $0.0075 attributed to the job in `api_logs` |
| Sandbox isolation | real Docker: non-root, `CapEff: 0000000000000000`, cap-drop enforced, `--network none`, `--rm` leaves nothing |
| Checkout → edit → patch | real git against a real repository: pinned commit materializes the right tree, an agent edit and a new file both appear in the patch, no remote is left behind |
| Worktree handover to the sandbox user | real Docker: root creates a `0700` worktree, chowns it to 10001, and a `--user 10001` container writes to it and produces a diff. Without the chown the same container gets `Permission denied` and `fatal: not a git repository` |
| The gateway on staging | `/v1/agent/*` live: job create/get/list/cancel, SSE stream, and a non-dispatcher key refused at `/worker/claim` with 401 |
| **A self-hosted runner completing a real job** | `ajob_04de5d509a25ded2` against staging: claimed → cloned `psf/requests` at `414f0513` (verified equal to that repo's HEAD) → Claude Code 2.1.220 in a Docker sandbox as uid 10001 → `Read` + `Edit` with `is_error: false` → 518-byte patch stored → `succeeded`. The patch was read back and matches the file the agent left on disk |

## Not verified

Be precise about these when reporting status:

- **The full model matrix.** One real job on one real model is verified (see
  above); the 2-runtime × 3-model matrix has not been run, so cross-model
  behaviour differences are still unknown.
- **The Actions workflow has never executed on GitHub.** See the blockers.
- **Kata.** Still a shared-kernel container in every run so far; the
  `--runtime` flag provably reaches the daemon but no job has run under an
  actual Kata kernel.
- **Production.** `/v1/agent/*` is live on staging; production has not been
  deployed from it — production returns 404 on those routes today.
- **Staging's model surface is thin.** Of the 15 models `/v1/models` lists,
  only `glm-5.1` and `qwen3.6-35b` actually resolve for an agent-job token, and
  both land on `qwen3.6-35b`. Everything else answers `404 Model not found`, so
  a job that names one fails at its first turn. Pick a model that resolves
  before concluding anything about the chain.
- **Kata.** Isolation was verified on a shared-kernel container. The Kata path
  is wired correctly — the daemon accepts `--runtime io.containerd.kata.v2` and
  proceeds to start the shim, failing only because this host has no shim
  binary — but no job has run under an actual Kata kernel.

  The three cases give three distinct errors, which is what makes this
  meaningful rather than hopeful:

  | `--runtime` | Result |
  |---|---|
  | unset (runc) | runs |
  | a name that does not exist | `unknown or invalid runtime name` — the daemon rejects it, so the flag is reaching the daemon and is not being silently dropped |
  | `io.containerd.kata.v2` | `failed to start shim` — the name is accepted and the shim is attempted; only the binary is missing |

  A dropped flag would be the dangerous failure: the kata backend would
  silently run as a plain shared-kernel container while every log line and
  config said otherwise. That is ruled out.

## Who may run a job against which repository

Two independent entitlements, and a repository qualifies on either. Both fail
closed: with neither, no job can be created at all.

**A connection the user made.** They authorize the GitHub App, GitHub returns a
code to `<frontend>/agents/connected`, and the platform exchanges it for a
token that speaks *as that user* to ask which installations they can reach.
Those installation ids are recorded against the user, and the repositories they
cover are what the composer offers. Every step is GitHub's answer rather than
the requester's — which is the point, because the requester chooses the
repository and the *platform* mints the credential for it. Anything the
requester could simply assert would be a confused deputy.

**The deployment allowlist** (`AGENT_REPO_ALLOWLIST`), for the single-tenant
dogfood where the repository is the operator's own and there is no user to
connect.

Disconnecting removes the platform's half. Uninstalling the App on GitHub is
the other half, and is what actually revokes the platform's reach.

## Before a job can run for real

Two things are outside the code and must be done by a human.

### 0. A closed agent network needs the gateway on it

`platform_only` means "our gateway and nothing else", which assumes the gateway
is *on that network* — true when it is the compose `backend` service, false the
moment `AGENT_GATEWAY_URL` points at a remote one. The sandbox then resolves
nothing and every job dies at its first model call, with an error that reads
like a broken model.

Preflight now probes this: it starts one container on the agent phase's real
network and asks whether the gateway host resolves, refusing at startup if not.
For a remote gateway, give the agent phase a network that routes to it:

```bash
AGENT_EGRESS_AGENT_TIER=custom
AGENT_EGRESS_NETWORK_CUSTOM=agent-routable
```

One more compose detail worth knowing: the overlay *declares* `agent-egress`,
so compose insists on creating it. A network of that name created by hand
beforehand is refused with a label mismatch — let compose own it, or point the
tier variables at names compose does not declare.

### 1. The workflow must be on the default branch

A workflow is only *registered* once its file exists on the default branch
(`main`). Until then it cannot be triggered **by any means** — not
`repository_dispatch`, not the schedule, and not `workflow_dispatch` even with
an explicit `--ref dev`. Verified rather than assumed:

```console
$ gh workflow list --all | grep agent-job-runner      # absent
$ gh api -X POST .../workflows/agent-job-runner.yml/dispatches -f ref=dev
{"message":"Not Found","status":"404"}
```

So the Actions path is gated on a production release, not on configuration.

Self-hosted runners have no such constraint: they poll `/v1/agent/worker/claim`
and can run as soon as the gateway is deployed. That is the shorter path to a
first real job.

### 2. Credentials

**Dispatcher credential** — set the same `AGENT_DISPATCHER_TOKEN` on the
gateway and the runner; the claim gate accepts it directly.

```bash
# both sides — one value, one purpose
AGENT_DISPATCHER_TOKEN=$(openssl rand -hex 32)
```

Prefer this over pointing the runner at `ADMIN_TOKEN` (which the gate still
accepts, for migration): the runner host executes untrusted repository code
next door, and the credential it holds should open exactly one door. The
dedicated token claims work and does nothing else — to every other route it is
an invalid credential, and rotating it touches nothing but the two settings
above.

It must not be an ordinary user key: that endpoint takes the oldest queued job
**across all tenants** and returns its repo, prompt, and a working capability
token, so an ordinary key there would be a cross-tenant read.

**GitHub credential** — install a GitHub App and give the *gateway* its private
key. Nothing needs to be minted or rotated by hand: the platform signs a
ten-minute App JWT and exchanges it for an hour-long token scoped to the
installation covering the repository being published to.

```bash
AGENT_GITHUB_APP_ID=123456
# A path the gateway can open — which in a Compose deployment means a path
# inside the *backend container*. It mounts `config/`, `distributions/` and
# `var/data/`, and nothing else: an /etc path on the host reads as "no such
# file" from in there. `var/data` is gitignored, so the key survives the
# deploy's `git reset --hard` and is never a candidate for commit.
AGENT_GITHUB_APP_PRIVATE_KEY_PATH=/app/var/data/agent-app.pem
```

Put the file at `<APP_DIR>/var/data/agent-app.pem` on the host, owned by the
service user and `chmod 600`. Alternatively `AGENT_GITHUB_APP_PRIVATE_KEY`
takes the PEM inline (escaped `\n` are accepted and unescaped), which avoids
the mount question entirely at the cost of a very long line in `.env`.

The App needs `contents: write` and `pull_requests: write` and nothing else —
notably not `workflows`, so a patch touching `.github/` cannot be pushed even
if the gate were bypassed. Revocation is uninstalling the App.

For the *connect* flow (a user authorizing their own repositories) the App
also needs its OAuth half, with the callback pointing at this deployment's
frontend:

```bash
AGENT_GITHUB_APP_CLIENT_ID=Iv1.xxxxxxxx
AGENT_GITHUB_APP_CLIENT_SECRET=...
AGENT_GITHUB_APP_INSTALL_URL=https://github.com/apps/<app-slug>/installations/new
# App setting "Callback URL": <FRONTEND_URL>/agents/connected?provider=github
```

Note `FRONTEND_URL` is read from `.env`, which is applied last and so wins
over the distribution overlay — check the host's value rather than the
overlay's when composing the callback.

The Integrations page derives the normal Connect/Reauthorize link from
`AGENT_GITHUB_APP_CLIENT_ID` and GitHub's web OAuth endpoint. The install URL
is used only after OAuth confirms that the user cannot see an existing App
installation. Keeping those two URLs separate is required for reconnects:
GitHub sends an already-installed App's `/installations/new` link to its
settings page without invoking the OAuth callback.

That same redirect is what *Configure repositories* relies on, so the install
URL stays wired to a connected user's Manage menu: it is the only route to the
page where the App's repository selection changes, and GitHub picks the user
or organization settings page itself. Set `AGENT_GITHUB_APP_INSTALL_URL` to
the `/apps/<slug>/installations/new` form — pointing it at the OAuth endpoint
instead leaves a user with no installation looping through authorization.

The same App also supplies the runner's clone credential, and the two are
*not* the same token. An installation token inherits every permission the App
holds unless it asks for less, so the clone token is requested as
`contents: read` on the single repository being worked on. Getting that wrong
would put a push-capable credential on the host that executes untrusted
repository code.

With no App configured the claim returns no clone token and the runner clones
anonymously, which is enough for a public repository. A private repository
needs the App.

A static `AGENT_GITHUB_TOKEN` still works as a fallback, but it is a
long-lived credential someone has to create and rotate; prefer the App.
With neither, the publisher loop idles and says so — jobs still run and still
produce patches, they just never become PRs.

## Running self-hosted

The runner must be a **standing service**, not a process someone starts by
hand: a queued job waits until something claims it, and "someone's laptop had
the runner up that afternoon" is how the first real job actually ran.

**On the staging host the deploy pipeline owns this.** `deploy_staging.sh`
enables the runner overlay whenever the host's `.env` sets
`AGENT_DISPATCHER_TOKEN` — the credential the runner needs anyway, so there is
no second switch to forget. Opting a host in is a one-time `.env` edit:

```bash
AGENT_DISPATCHER_TOKEN=<openssl rand -hex 32>   # same value gates /worker/claim
AGENT_SANDBOX_BACKEND=container                  # host has no Kata shim yet
```

Every subsequent deploy then builds the sandbox image and brings the runner up
in the **same compose invocation** as the main stack. Same-invocation is a
correctness requirement, not a convenience: the overlay attaches `backend` to
the agent-egress network, and a separate compose call without the distribution
env files would recreate backend stripped of its site identity.

For a machine that is *not* the staging host (a dedicated runner box, a dev
machine), the script below does the same by hand — one command per host makes
it standing; compose's `restart: unless-stopped` plus an enabled Docker daemon
carries it across crashes and reboots:

```bash
ops/deploy/agent_runner.sh up 4      # build images, start 4 runners
ops/deploy/agent_runner.sh status    # replicas + recent log tail
ops/deploy/agent_runner.sh down      # stop them; the main stack is untouched
```

### How many runners

**One runner runs one job at a time.** It claims, runs the job to completion,
then claims the next — so the replica count *is* how many jobs the deployment
can run concurrently, and everything else waits in the queue. With one
replica, two users are serialised; a job that runs to its hour-long timeout
holds up everyone behind it.

Set it with `AGENT_RUNNER_REPLICAS` (default 3). Replicas need no
coordination — `claim_job` uses `FOR UPDATE SKIP LOCKED`, so they share one
queue with no leader and no sharding — and each job's sandbox is capped at
4g / 2 cpus, so budget roughly that per replica.

Do not scale with a `--scale` flag instead: it survives exactly until the next
`docker compose up` without it, which drops the fleet back to one and presents
as "every user is queueing" long after anyone remembers scaling it.

Queue order is global FIFO with no per-user fairness yet, so one user
submitting a batch can occupy every replica. Per-user concurrency quotas are
P1 work.

The script validates the required environment before starting and surfaces
the runner's own preflight verdict (gateway reachable, image spawnable,
workdir bind-mountable, egress network resolvable) instead of leaving it in a
detached log. Equivalent by hand:

```bash
docker build -f deploy/docker/Dockerfile.agent-sandbox \
             -t hybridinference-agent-sandbox:latest .

AGENT_SANDBOX_IMAGE=hybridinference-agent-sandbox:latest \
  docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.agent-runner.yml \
               up -d --scale agent-runner=4
```

Scaling is only `--scale`: `claim_job` uses `FOR UPDATE SKIP LOCKED`, so
runners share one queue with no leader and no sharding.

| Variable | Default | Notes |
|---|---|---|
| `AGENT_SANDBOX_BACKEND` | `kata` | `process` (no isolation) refuses to start unless `AGENT_SANDBOX_ALLOW_UNISOLATED=1` |
| `AGENT_SANDBOX_IMAGE` | required | Image built from `Dockerfile.agent-sandbox` or an equivalent deployment-owned image |
| `AGENT_SANDBOX_NETWORK` | `agent-egress` | Declared `internal: true`, so a sandbox reaches the gateway and nothing else. Also the `platform_only` network unless `AGENT_EGRESS_NETWORK_PLATFORM_ONLY` overrides it |
| `AGENT_EGRESS_SETUP_TIER` / `_AGENT_TIER` | `platform_only` | One of `platform_only` / `trusted` / `custom` / `full`, **per phase**. The design's external-beta shape is setup=`trusted`, agent=`platform_only`; the overlay ships both closed because there is no setup phase yet and no allowlist-fronted network to run one on |
| `AGENT_EGRESS_NETWORK_*` | — | Network per tier. A tier with no network is an error when a phase selects it, never a fall back to a more open one |
| `AGENT_SNAPSHOT_ROOT` | — | Where setup snapshots live. Unset disables caching, so every job reinstalls. Bind it at the same path inside and out, like the worktrees |
| `AGENT_SNAPSHOT_TTL_S` | `604800` | Seven days, as the design specifies. A stale entry means a wrong dependency tree |
| `AGENT_EGRESS_ALLOWLIST` | — | Checked at startup: it may not contain an agent vendor's telemetry domain, which would let a "closed" sandbox report on the repository it was given |
| `AGENT_WORKDIR_ROOT` | `/var/lib/hybridinference/agent-jobs` | **A host path, bind-mounted at the same path inside the runner.** Preflight test-mounts it and fails at startup if not — otherwise every job dies at spawn with an opaque exit 125 |
| `AGENT_SANDBOX_UID` / `_GID` | `10001` | Only for a custom sandbox image; must match its user |
| `AGENT_REPO_ALLOWLIST` | — | Comma-separated `owner/name`, or `owner/*`, for the single-tenant dogfood. Users who connect the App themselves do not need it; unset simply means the only entitlement is a user's own connection |
| `AGENT_GITHUB_APP_CLIENT_ID` / `_CLIENT_SECRET` | — | The App's OAuth half. Only the user-facing connect flow needs it; minting installation tokens uses the private key alone |
| `AGENT_GITHUB_APP_INSTALL_URL` | — | GitHub App installation URL, `https://github.com/apps/<slug>/installations/new`. Used when OAuth finds no accessible installation, and behind *Configure repositories* for a connected user. The gateway adds a short-lived, user-bound `state`; set the App callback to `<frontend>/agents/connected?provider=github` |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` | — | GitLab.com OAuth application credentials, kept on the gateway. Configure only `read_user` and `read_api` scopes |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI` | — | Exact registered callback, normally `<frontend>/agents/connected?provider=gitlab`. HTTPS is required except for localhost development |
| `AGENT_SANDBOX_ALLOW_OPEN_NETWORK` | — | Accepts a non-`internal` sandbox network. Preflight refuses one otherwise, so a missing setting cannot quietly mean full egress |
| `AGENT_GITHUB_TOKEN` | — | Gateway-side; unset means the publisher idles |
| `AGENT_PUBLISH_BASE_BRANCH` | `dev` | What draft PRs target |

The authenticated `/agents/integrations` page is the only supported place to
start either OAuth flow. OAuth state is unpredictable, bound to the signed-in
user and provider, expires after ten minutes, and is consumed once. GitLab also
uses PKCE. GitLab access and refresh tokens are encrypted before persistence,
rotated on refresh, never returned to the browser, and revoked best-effort on
disconnect.

GitHub repositories retain the complete Agent lifecycle: checkout, branch
discovery, push, and draft PR publishing use short-lived GitHub App installation
tokens. GitLab is intentionally narrower in this release: it verifies the
authenticated GitLab user and shows their accessible projects for discovery.
GitLab projects do not appear in the Agent task composer, and the gateway does
not claim to read their source or publish GitLab merge requests yet.

### Three things about this topology that look like details and are not

**The runner is not the gateway image.** It builds from
`Dockerfile.agent-runner`, which adds a Docker client (the container backend
shells out to `docker` to start each sandbox) and `git` (the runner checks the
repository out). The gateway image has neither, and a runner built from it
fails preflight on every host.

**The job worktree must be a host path with the same name on both sides.** The
runner asks the daemon to bind-mount each job directory into the sandbox, and
the daemon resolves that path on the *host* — not inside the runner container.
A named volume works for the runner's own file operations and then fails every
`docker run --mount` with exit 125.

**The runner runs as root, and that is the design.** It creates each job
worktree, checks the repository out, and then chowns the tree to uid 10001 —
the sandbox image's user — before mounting it. Skip that and the agent cannot
write a single file, and git refuses to look at the repository at all
("dubious ownership"). An unprivileged runner falls back to making the tree
world-writable and logs `agent_sandbox_workdir_world_writable`; that is only
defensible on a dedicated single-purpose host.

The isolation boundary is between the runner and the sandbox it starts, not
around the runner. Running the runner unprivileged does not buy isolation — it
already holds the dispatcher credential and the Docker socket.

## Runtime verification

Run the adapter contract tests after changing runtime integration code. They
replay recorded and synthetic events to pin normalized mapping without a live
model or deployment credential:

```bash
uv run pytest tests/unit/test_agent_runtimes.py
```

These tests do not invoke the installed agent binaries. An installed-CLI smoke
test and full live-model matrix belong to the deployment operating those
binaries, models, and credentials. Keep that conformance configuration in the
deployment overlay, not in the neutral upstream repository.

## Known gateway findings

Surfaced by the conformance suite, tracked separately from this feature:

- An upstream mid-stream disconnect is masked with a synthesized
  `finish_reason: stop` + `[DONE]`, so a client cannot detect truncation. For
  agent workloads that is a poisoning vector: truncated tool-call arguments
  look complete.
- An in-stream 429 error frame is typed `server_error`, so a client branching
  on `type` to decide whether to back off misclassifies a rate limit.

## Model behaviour observed on the first real run

`deepseek-v4-flash` replied *"I've added a docstring to the `greet` function.
It now reads: ..."* immediately after receiving a tool result with
`is_error: true` saying the write had been denied. Nothing had changed on
disk.

This is why the runner reports what the tools did rather than what the agent
says about them: it collects errored `tool_result` entries, emits them into
the owner's stream, and refuses to record a run as successful when it produced
no patch *and* a tool failed. A model's own account of its work is not
evidence.
