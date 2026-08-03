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
- **Production.** `/v1/agent/*` is live on staging; production has not been
  deployed from it — production returns 404 on those routes today.
- **Staging's model surface is thin.** Of the 15 models `/v1/models` lists,
  only `glm-5.1` and `qwen3.6-35b` actually resolve for an agent-job token, and
  both land on `qwen3.6-35b`. Everything else answers `404 Model not found`, so
  a job that names one fails at its first turn. Pick a model that resolves
  before concluding anything about the chain.
- **Kata.** No job has run under an actual Kata kernel, so every run so far
  shared the host's. Isolation was verified on a shared-kernel container.

  What changed is that the host side is now provisionable rather than absent.
  `ops/setup/setup_kata_runtime.sh` installs and links a pinned Kata release,
  and `ops/deploy/agent_runner.sh preflight` refuses to start runners until one
  real container comes back reporting a kernel that is *not* the host's. Both
  are covered by unit tests, including the case where the runtime is accepted
  and the sandbox still lands on the host kernel.

  Neither has been run on the staging runner host. Installing into `/opt/kata`
  is a root-level change to a live machine, so it is the next step rather than a
  completed one — and until it happens, this bullet stays here. See
  [Provisioning Kata on a runner host](#provisioning-kata-on-a-runner-host).

  The daemon-side wiring was already ruled out as the problem. The three cases
  give three distinct errors, which is what makes this meaningful rather than
  hopeful:

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

Preflight probes this: it starts one container on the agent phase's real
network and **makes a request**, refusing at startup if no HTTP response comes
back. Any status counts, 401 and 404 included — the question is whether packets
arrive, not what the gateway makes of them. It used to stop at DNS, which
passes for anything holding a record, including a relay that answers to the
gateway's name and cannot reach the gateway behind it; that is exactly the
shape of a cross-machine deployment, so the topology needing the check most was
the one it could not see. A sandbox image with neither `curl` nor `python3`
falls back to DNS and logs `agent_sandbox_gateway_probe_degraded` rather than
letting the weaker check pass for the stronger one.

For a runner host that is not the gateway's, see
[Running runners on another machine](#running-runners-on-another-machine) — the
supported answer is a tunnel on the closed network, not a routable one. Opening
the sandbox's network instead is possible and is a real cost:

```bash
AGENT_EGRESS_AGENT_TIER=custom
AGENT_EGRESS_NETWORK_CUSTOM=agent-routable
```

That gives the agent phase general egress — untrusted repository code included
— which is the property `platform_only` exists to hold.

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

That second line is a **deliberate downgrade**, and the deploy now says so on
every run: it puts every job on the host's own kernel. It was the price of
getting a standing runner up before any host had Kata. Remove it once the host
is provisioned — see below — and the deploy will verify the boundary instead of
warning about its absence.

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
ops/deploy/agent_runner.sh preflight # check the isolation boundary, start nothing
ops/deploy/agent_runner.sh status    # replicas + recent log tail
ops/deploy/agent_runner.sh down      # stop them; the main stack is untouched
```

### Provisioning Kata on a runner host

An ordinary container isolates processes; every sandbox still issues syscalls
straight at the host kernel. Kata gives each job a lightweight VM with its own
kernel, so a container escape has a hypervisor behind it. That is what makes the
difference between "fine for repositories we trust" and "fine for a repository
we have never read", because the sandbox runs whatever the repository's build
and test scripts do.

The runtime belongs to the **host**, not the runner image: the runner container
holds only a Docker client and talks to the host's daemon, and it is that daemon
that has to resolve `io.containerd.kata.v2`. Nothing in the container image can
supply it. One command per host, needing root:

```bash
sudo ops/setup/setup_kata_runtime.sh
```

It is idempotent, checks the host can actually start VMs (`/dev/kvm`, CPU
virtualization extensions) before downloading anything, verifies the release
against a pinned SHA-256, and links the shim into `/usr/local/bin` so
containerd finds it without a unit-file edit. The version is pinned in the
script; upgrading is a two-line change there, and `--check` then fails on every
host still carrying the old one.

Requirements: an x86_64 Linux host, bare metal or with nested virtualization
enabled. Kata cannot run without KVM.

Afterwards, drop `AGENT_SANDBOX_BACKEND=container` from the host's `.env` and
redeploy. Both `agent_runner.sh up` and `deploy_staging.sh` then gate on it:

- with the default `kata` backend, the shim must be installed, **and** one real
  container started from the sandbox image must report a kernel that is not the
  host's. A runtime the daemon accepts while the sandbox still lands on the host
  kernel fails this — that is the case every cheaper signal misses.
- there is no automatic fallback to a shared kernel. Accepting one stays
  possible and stays explicit: set `AGENT_SANDBOX_BACKEND=container`, and every
  deploy restates what it costs.

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
| `AGENT_SANDBOX_BACKEND` | `kata` | `process` (no isolation) refuses to start unless `AGENT_SANDBOX_ALLOW_UNISOLATED=1`. On the default `kata`, deploy refuses until the host has the shim and one real container proves it gets its own kernel; `container` is an explicit, and loudly restated, shared-kernel downgrade |
| `AGENT_SANDBOX_IMAGE` | required | Image built from `Dockerfile.agent-sandbox` or an equivalent deployment-owned image |
| `AGENT_SANDBOX_NETWORK` | `agent-egress` | Declared `internal: true`, so a sandbox reaches the gateway and nothing else. Also the `platform_only` network unless `AGENT_EGRESS_NETWORK_PLATFORM_ONLY` overrides it |
| `AGENT_EGRESS_SETUP_TIER` / `_AGENT_TIER` | `platform_only` | One of `platform_only` / `trusted` / `custom` / `full`, **per phase**. The design's external-beta shape is setup=`trusted`, agent=`platform_only`; the overlay ships both closed because there is no setup phase yet and no allowlist-fronted network to run one on |
| `AGENT_EGRESS_NETWORK_*` | — | Network per tier. A tier with no network is an error when a phase selects it, never a fall back to a more open one |
| `AGENT_SNAPSHOT_ROOT` | — | Where setup snapshots live. A snapshot holds only what the setup script added to the worktree — never the checkout — and is keyed by repository, script, and sandbox image. Unset disables caching, so every job reinstalls. Bind it at the same path inside and out, like the worktrees |
| `AGENT_SNAPSHOT_TTL_S` | `604800` | Seven days, as the design specifies. A stale entry means a wrong dependency tree |
| `AGENT_EGRESS_ALLOWLIST` | — | Checked at startup: it may not contain an agent vendor's telemetry domain, which would let a "closed" sandbox report on the repository it was given |
| `AGENT_WORKDIR_ROOT` | `/var/lib/hybridinference/agent-jobs` | **A host path, bind-mounted at the same path inside the runner.** Preflight test-mounts it and fails at startup if not — otherwise every job dies at spawn with an opaque exit 125 |
| `AGENT_RUNNER_HOST` | — | Names the *machine*, shared by its replicas: the unit the admin host switch picks between. `agent_runner.sh` and `deploy_staging.sh` fill it from the host's own name. Never derived inside the container, where the hostname is a container id. Unset keeps the machine out of the pool and changes nothing else |
| `AGENT_SANDBOX_UID` / `_GID` | `10001` | Only for a custom sandbox image; must match its user |
| `AGENT_REPO_ALLOWLIST` | — | Comma-separated `owner/name`, or `owner/*`, for the single-tenant dogfood. Users who connect the App themselves do not need it; unset simply means the only entitlement is a user's own connection |
| `AGENT_GITHUB_APP_CLIENT_ID` / `_CLIENT_SECRET` | — | The App's OAuth half. Only the user-facing connect flow needs it; minting installation tokens uses the private key alone |
| `AGENT_GITHUB_APP_INSTALL_URL` | — | GitHub App installation URL, `https://github.com/apps/<slug>/installations/new`. Used when OAuth finds no accessible installation, and behind *Configure repositories* for a connected user. The gateway adds a short-lived, user-bound `state`; set the App callback to `<frontend>/agents/connected?provider=github` |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` | — | GitLab.com OAuth application credentials, kept on the gateway. Configure only `read_user` and `read_api` scopes |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI` | — | Exact registered callback, normally `<frontend>/agents/connected?provider=gitlab`. HTTPS is required except for localhost development |
| `AGENT_SANDBOX_ALLOW_OPEN_NETWORK` | — | Accepts a non-`internal` sandbox network. Preflight refuses one otherwise, so a missing setting cannot quietly mean full egress |
| `AGENT_GITHUB_TOKEN` | — | Gateway-side; unset means the publisher idles |
| `AGENT_PUBLISH_BASE_BRANCH` | `dev` | What a draft PR targets when its job named no branch of its own. A job started from a branch targets *that* branch; this is only the fallback |

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

### Which machine runs the jobs

**Admin → Settings → Cloud Agent Host.** The list is every machine that has
polled for work, and the radio button picks the one whose runners may claim.
"Any host" is the default and means what it always meant: whoever polls first
takes the job.

Runners *pull*, so this is a gate on the claim rather than a dispatch target —
the gateway cannot push a job at a machine, and the only moment it gets to say
"not you" is when a runner asks for work. Three consequences worth knowing
before using it:

- **The switch is not preemptive, and not a drain.** The new host starts
  claiming immediately; jobs already running on the old one keep running to the
  end, holding their leases. The two overlap — a drain would mean waiting for
  the old host to empty first, and this does not do that. Expect the old host
  to stay busy for as long as its longest running job.
- **A runner reporting no host is refused while any host is pinned.** Failing
  closed is deliberate: leaving the machine you just switched away from able to
  claim would make the switch a lie. A runner without `AGENT_RUNNER_HOST` set
  therefore stops taking work the moment anything is pinned.
- **A machine joins the pool by polling, not by being registered.** Start a
  runner there and it appears within seconds — including while it is being
  turned away, which is what makes it selectable in the first place. Pinning to
  a name nothing has ever polled from is refused, because the symptom is a
  queue that hangs with nothing in the logs.
- **A host name is a scheduling label, not a machine identity.** The runner
  reports it, so anything holding `AGENT_DISPATCHER_TOKEN` can report any name;
  two machines configured alike are one entry in the list. The credential is
  the actual boundary. Use this to decide where *our* machines run *our* jobs,
  and do not build anything on it that must survive a hostile runner.
- **There is no failover.** Pinning turns the other machines off, so if the
  pinned host's runners stop, the queue stops with them — nothing takes over.
  The admin page warns when the pinned host has not polled recently, but that
  signal is imperfect in the other direction too: `last_seen` is refreshed by
  claims alone, so a machine whose runners are all busy on long jobs looks
  exactly like one that is down. An independent heartbeat is the fix and is not
  built yet.

Adding a *second* machine is not only this switch. A sandbox reaches the
gateway over a network declared `internal: true`, which resolves nothing off
its own host — so a runner box that is not the gateway's host needs the
routable-network configuration from
[§0 above](#0-a-closed-agent-network-needs-the-gateway-on-it) (or its own
gateway). Preflight refuses at startup rather than failing every job at its
first model call, but it refuses *there*, on that machine, not here.

### Running runners on another machine

A second machine is worth adding for capacity, or because it is the one with
the hardware. What makes it more than "run the script over there" is that a
sandbox reaches the gateway over a network declared `internal: true` — no route
off the host at all — which works only because the gateway is normally a
container on that same network. On a machine with no gateway, that network is
empty and every job dies at its first model call.

The answer is a **fixed-destination tunnel on the closed network**:

```text
runner host                                    gateway host
┌───────────────────────────────────┐
│ [sandbox] ─┐                      │
│            ├→ agent-gateway-tunnel│──ssh──→ 127.0.0.1:8080
│ [runner] ──┘        ↑             │
│   ✗ internet   one address, fixed │
└───────────────────────────────────┘
```

The sandbox's world is unchanged: one endpoint, ours. The tunnel is a relay and
not a proxy — no CONNECT, no caller-chosen destination — so nothing the agent
sends can widen it.

**Why ssh rather than exposing the gateway's port.** Every claim carries
`AGENT_DISPATCHER_TOKEN` and every job call carries that job's capability
token. Binding `8080` to the lab network puts both in cleartext on a shared
segment. The tunnel keeps the gateway listening on loopback, which is where it
already listens.

```bash
ops/deploy/agent_remote_runner.sh up 4     # tunnel + 4 runners
ops/deploy/agent_remote_runner.sh check    # probe the gateway from the closed network
ops/deploy/agent_remote_runner.sh status
ops/deploy/agent_remote_runner.sh down
```

**Use that script, not `agent_runner.sh`.** The latter layers its overlay on
the main compose file, whose `agent-runner` declares `depends_on: backend` —
pointed at a machine with no gateway it does not fail, it starts one there.
`docker-compose.agent-remote-runner.yml` is standalone and contains only the
runners, their workspace broker, the tunnel, and the two networks.

| Variable | Notes |
|---|---|
| `AGENT_TUNNEL_SSH_DESTINATION` | `user@gateway-host`. The only address anything on this machine can reach through the tunnel |
| `AGENT_TUNNEL_SSH_KEY` | Private key, mounted read-only. Give it its own key and restrict it on the gateway: `command="",restrict,permitopen="127.0.0.1:8080"` — a runner host needs one forwarded port, not a shell |
| `AGENT_TUNNEL_SSH_KNOWN_HOSTS` | The gateway's host key. **Required**, and host checking is not disableable: an unverified hop would hand the dispatcher credential to whatever answers on that address, which is the attack encrypting it is meant to prevent |
| `AGENT_TUNNEL_REMOTE_PORT` / `_HOST` | Where the gateway listens *on its own machine*; `127.0.0.1:8080` by default |
| `AGENT_GATEWAY_HOSTNAME` | The name both the runner and the sandbox resolve the tunnel by, `agent-gateway` by default. **Do not override `AGENT_GATEWAY_URL` with the gateway's real address:** the runner would reach it and the sandbox, on the closed network, would not — jobs would be claimed and then die at their first model call |

Generate the known_hosts entry deliberately, and look at it:

```bash
ssh-keyscan -p 22 <gateway-host> > /etc/agent-tunnel/known_hosts
```

Then `agent_remote_runner.sh check` answers the only question that matters, from
the only vantage point whose answer means anything — a container on the closed
network, asking the gateway for `/health`. Reaching the gateway from the host's
own shell proves nothing about what a sandbox can reach.

Once it is up, the machine appears in **Admin → Settings → Cloud Agent Host**
and can be selected there.

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

### Dependency installation, and the proxy that allows it

A job may carry a `setup_script`. It runs before the agent, under its own
egress tier, and its result is cached per repository and script — so a retry
does not reinstall.

The tier is where the work is. `setup` defaults to `trusted` and `agent` stays
`platform_only`, which in this deployment means three networks:

| Network | Internal | Who is on it |
|---|---|---|
| `agent-egress` | yes | the sandbox during the agent turn, and the gateway |
| `agent-egress-trusted` | yes | the sandbox during setup, and the proxy |
| `agent-egress-uplink` | **no** | the proxy, alone |

Both sandbox networks have no route of their own. The difference is that
`agent-egress-proxy` sits on the trusted one with a second leg, and the sandbox
is handed `http_proxy`/`https_proxy` pointing at it. The environment variables
are a convenience for package managers, not the boundary: a setup script that
ignored them finds a network that cannot route anywhere.

The agent turn — the part driven by untrusted model output — still reaches the
gateway and nothing else. That does not change when setup is opened.

**Adding a domain.** `AGENT_EGRESS_ALLOWLIST` is *added to* the built-in
registry list (PyPI, npm, crates, Go, RubyGems, Maven, github.com), never a
replacement for it. `*.example.com` covers subdomains. An agent vendor's
telemetry domain is refused outright — a sandbox that can phone home about a
private repository is not closed, whatever the tier is called. The list is
rendered into the proxy's config by `agent-egress-proxy-config`, a one-shot
service that runs before the proxy and fails the deploy if the list is
unusable. Editing that config by hand skips the validation that keeps a
hostile value out of it.

**Three preflight failures and what each means.** The runner refuses to claim
jobs unless the setup tier is genuinely enforcing:

- *"the sandbox network … is not `internal`"* — a tier variable points at a
  routable network. The setup phase would have the whole internet while the
  config still said `trusted`.
- *"egress proxy is unreachable"* — the proxy is not running, or not on that
  network. Check `docker compose logs agent-egress-proxy`; a config Squid
  rejects shows up there as a parse error on the first line. This one stops the
  runner claiming **any** job, including jobs that install nothing — deliberate,
  because it means the deploy is broken, but if this host genuinely does not
  need dependency installation, `AGENT_EGRESS_SETUP_TIER=platform_only` skips
  the tier and its probe entirely.
- *"egress proxy answered … it is not enforcing an allowlist"* — the proxy
  served a host that does not exist and is on no list. Treat this as an open
  proxy on the sandbox's network.

**Where a denied request is visible.** Only in the proxy's log — an internal
network refuses a connection silently, so before this there was nowhere to
observe a blocked attempt from. `ops/deploy/agent_runner.sh status` tails it;
a denial is the line with `verdict=TCP_DENIED/403`.

**What this does not give you.** No secrets reach the sandbox (there is still
no secrets store, by design), the sandbox runs unprivileged so `apt install`
does not work — use `pip --user`, a virtualenv, `npm ci` — and a tool that
ignores the proxy environment (bun, at the time of writing) cannot reach a
registry at all.

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
