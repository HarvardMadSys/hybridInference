# Cloud Agents

Run a coding agent on your GitHub repository, from the browser. Describe a
task, and FreeInference runs the agent in an isolated cloud sandbox, streams
its progress live, and opens a **draft pull request** for you to review.
Nothing merges without you.

> **Beta:** Cloud agents are rolling out gradually. If the Agents page is not
> enabled on your account yet, the workflow below is what ships as it opens up.

## Why cloud agents on FreeInference

Hosted coding agents usually lock the agent and the model together — one
vendor's agent, driving that vendor's models. FreeInference decouples the two:

**Bring your agent**
: Each task picks its runtime. Claude Code and Codex are supported today, and
  the platform is runtime-agnostic by design — new runtimes onboard without
  changing the security model.

**Bring your model**
: The same task can be driven by any model from the [model catalog](models.md)
  that is enabled for agent tasks — pick per task, right in the composer.

**Honest metering**
: Every model call flows through the FreeInference gateway, so usage is
  measured by the platform. Nothing depends on an agent's self-reported
  numbers.

## Quickstart

### Step 1: Connect GitHub

1. Sign in at [https://freeinference.org](https://freeinference.org) and open
   **Agents**
2. Open **Integrations** and choose **Connect GitHub**
3. Install the FreeInference GitHub App, selecting which repositories it may
   access
4. Back on the Agents page, those repositories now appear in the task composer

The App asks for the minimum a draft PR needs: repository contents and pull
requests. It has no access to your repository settings or CI workflows.

### Step 2: Describe a task

In the composer:

1. Describe the change you want, the way you would brief a colleague — e.g.
   *"Fix the SSE total-timeout regression on /v1/messages and add a unit test"*
2. Pick the **Repository** and the **Branch** to start from
3. Pick the **Runtime** (the agent) and the **Model** that drives it
4. Run the task

Good first tasks are self-contained code changes: a bug fix, a focused
refactor, tests for an under-covered module, a documentation sweep. See
[current limits](#current-limits) for what the sandbox deliberately cannot do
yet.

### Step 3: Watch it work

The task page streams the agent's activity as it happens — its reasoning, each
tool call with its result, and the diff as it takes shape. A running task can
be cancelled at any point.

### Step 4: Review the draft pull request

When the agent finishes, the platform validates its patch and publishes it to
a dedicated `agent/<job-id>` branch, then opens a **draft pull request** —
linked directly from the task page. Review and merge it like any other PR.
The agent never merges, and never pushes to branches you already have.

## The security model

The sandbox is designed so that a misbehaving agent — or a repository whose
contents try to hijack one — has the smallest possible blast radius:

- **Isolated, disposable sandbox.** Each task runs in its own sandbox as an
  unprivileged user, with all Linux capabilities dropped. The sandbox is
  destroyed after the run.
- **No internet access.** The sandbox's only network route is the
  FreeInference gateway, for model calls and progress events. It cannot reach
  anything else.
- **Your GitHub credentials never enter the sandbox.** The repository is
  checked out *outside* the sandbox with a read-only token scoped to that one
  repository. Inside, the agent holds exactly one credential: a job-scoped
  model key, budget-capped and revoked the moment the task ends.
- **The agent cannot push.** It produces a patch; the platform — not the
  agent — validates it and does the publishing. Patches are scanned for
  secrets, size-limited, and any change under `.github/` is rejected
  outright, so an agent cannot alter your CI workflows.
- **Draft PR or nothing.** The only write the platform ever performs on your
  repository is a fresh `agent/<job-id>` branch plus its draft pull request.

> **Note:** The draft PR's branch lives in your repository, so your own CI
> triggers apply to it like to any other branch. The platform guarantees the
> agent cannot *modify* your workflows; whether workflows *run* on agent
> branches is up to your repository's configuration.

## Models and usage

Agent tasks use your existing FreeInference account — there is no separate
signup. Model calls made by an agent are metered by the gateway per task and
count toward your account usage like your other API traffic. Every task also
runs under a budget cap, so a runaway agent loop is stopped by the platform
rather than by your patience.

The composer's model list is the source of truth for which models can drive
agent tasks; it can be a subset of the full [model catalog](models.md) while
the feature is in beta.

## Current limits

Deliberate beta-stage boundaries, so you can calibrate what to delegate:

- **No dependency installation.** The sandbox has no internet access, so a
  task cannot `npm install` / `pip install`, call external APIs, or browse
  the web. Choose tasks the repository's own contents can satisfy; a setup
  phase with cached dependencies is on the roadmap.
- **No secrets.** There is no way to pass credentials or environment secrets
  into a task — by design, for now.
- **Task execution is GitHub-only.** GitLab can be connected on the
  Integrations page for account and project discovery, but GitLab tasks and
  merge requests are not supported yet.
- **One repository per task.**

## FAQ

**A task sits in "Queued".**
Tasks wait for runner capacity during the beta. If a task does not start
within a few minutes, it will still run when capacity frees up — or cancel it
and retry later.

**The model I use in my IDE is not offered in the composer.**
The composer lists only the models enabled for agent tasks, which during the
beta is narrower than the full catalog.

**The task finished but produced no pull request.**
The task page's activity feed shows exactly what the agent's tools did. A run
that produced no patch — for example because tool calls failed — is reported
as failed rather than dressed up as a success; the feed is the place to see
why.

**How do I revoke access?**
Disconnect on the Integrations page, and uninstall the FreeInference GitHub
App in your GitHub settings. Uninstalling the App is what severs the
platform's access to the repositories it covered.
