# Cloud Agents

Describe a task, and FreeInference checks out your GitHub repository, runs a
coding agent on it in an isolated cloud sandbox, and opens a **draft pull
request** with the result. Each task picks which coding agent does the work
and which model drives it.

> **Beta:** Cloud agents are rolling out gradually. If the Agents page is not
> enabled on your account yet, the workflow below is what ships as it opens up.

## How it works

Every task moves through the same pipeline:

1. **Checkout.** The platform clones your repository at the branch you chose —
   outside the sandbox, with a read-only token scoped to that one repository.
   Your GitHub credentials never enter the sandbox.
2. **Run.** The agent works in a disposable sandbox that holds exactly one
   credential: a budget-capped model key valid for this task only. The sandbox
   has no internet access — its only network route is the FreeInference
   gateway, for model calls and progress events.
3. **Publish.** The agent cannot push. Its result is a patch, which the
   platform checks (secret scan, size limit, any change under `.github/` is
   rejected) before pushing it to a fresh `agent/<job-id>` branch and opening
   a draft pull request.
4. **Review.** Merging stays with you. The agent never merges and never
   touches branches you already have.

> **Note:** The draft PR's branch lives in your repository, so your own CI
> triggers apply to it like to any other branch. The agent cannot modify your
> workflows; whether workflows run on agent branches is your repository's
> configuration.

## Connect GitHub

One-time setup:

1. Sign in at [https://freeinference.org](https://freeinference.org) and open
   **Agents**
2. Open **Integrations** and choose **Connect GitHub**
3. Install the FreeInference GitHub App, selecting which repositories it may
   access
4. Back on the Agents page, those repositories appear in the task composer

The App asks only for repository contents and pull requests — the minimum a
draft PR needs. It has no access to repository settings or CI workflows. To
change which repositories are available later, edit the App installation on
GitHub.

To disconnect: remove the connection on the Integrations page, and uninstall
the App in your GitHub settings — uninstalling is what revokes repository
access.

## Run a task

In the composer:

1. Describe the change you want, the way you would brief a colleague — e.g.
   *"Fix the SSE total-timeout regression on /v1/messages and add a unit test"*
2. Pick the **Repository** and the **Branch** to start from
3. Pick the **Agent** and the **Model**
4. Run the task

The task page streams the agent's activity as it happens — its reasoning, each
tool call with its result, and the diff as it takes shape. **Stop** (or
**Escape** while focus is outside a text field) interrupts a working agent.
Any intermediate changes are saved but not published; send a follow-up in the
same task after it stops to restore those changes and continue in a new
isolated pass with the existing conversation as context.
Cancellation does not recall a result that is already being published. When
the task finishes, the draft pull request is linked directly from the task
page.

A task also takes **follow-up messages**: send one from the task page and it
runs as a new isolated pass in the same thread, inheriting the repository and
the agent of the turn before. Its **model** is chosen per turn: the control
beside the follow-up box starts on the model the last turn ran and switches
the next one onto any other listed model — so a thread can open on a fast
model and move to a larger one where the work turns out to be hard.

The conversation itself carries three actions. Every message has **Copy**.
**Fork from here** on any earlier answer duplicates the conversation up to
that turn into a new thread — history and not-yet-published changes carry
over, and the fork publishes to its own branch, so a different direction can
be tried without disturbing the original. **Edit & rewind** on one of your
own messages forks the conversation from just before it and prefills the
composer with that message, ready to resend changed — without the turns that
followed. Forking copies recorded history only: a reply still streaming stays
with the original task.

## Choosing an agent and model

The **agent** is the tool that does the work — Claude Code, Codex, OpenCode,
Kilo Code, and pi are available today. Claude Code and Codex stream fully
structured activity (reasoning, tool calls, diffs); OpenCode, Kilo Code, and
pi run with a plainer raw log view. The **model** is what drives the agent: the composer lists the
models enabled for agent tasks, which during the beta can be a subset of the
full [model catalog](models.md). Both are picked per task, and any listed
model can drive any agent.

As with interactive use, larger models suit gnarly, open-ended tasks; faster
models suit mechanical sweeps and small fixes.

## What to delegate

Cloud agents do best on self-contained changes that the repository's own
contents can satisfy: a bug fix with a clear description, a focused refactor,
tests for an under-covered module, a documentation sweep.

Beta-stage boundaries to keep in mind:

- **No dependency installation.** The sandbox has no internet access, so a
  task cannot `npm install` / `pip install`, call external APIs, or browse
  the web. A setup phase with cached dependencies is on the roadmap.
- **No secrets.** There is no way to pass credentials or environment secrets
  into a task.
- **Task execution is GitHub-only.** GitLab can be connected on the
  Integrations page for account and project discovery, but GitLab tasks and
  merge requests are not supported yet.
- **One repository per task.**

## Usage and budgets

Agent tasks use your existing FreeInference account. Model calls made by an
agent are metered by the gateway per task and count toward your account usage
like your other API traffic. Every task runs under a budget cap, so a runaway
agent loop is stopped by the platform.

## Security

The pipeline above *is* the security model; this section collects the
guarantees in one place:

- **Per-task isolation.** Each task gets its own sandbox, running as an
  unprivileged user with all Linux capabilities dropped, destroyed when the
  task ends. Tasks share nothing with each other.
- **Deny-all network.** The sandbox's only route is the FreeInference
  gateway. Repository contents are treated as untrusted input: even if they
  hijack the agent's instructions (prompt injection), there is no path to
  send your code or data anywhere else — the worst a hijacked run can
  produce is a bad patch, which arrives as a draft PR for your review, never
  a merge.
- **Credentials stay outside.** GitHub tokens live on the platform and never
  enter the sandbox; the checkout token is read-only and scoped to the one
  repository. The sandbox's only credential is the task's model key —
  budget-capped, valid for one task, revoked when it ends.
- **One write path.** The only write ever performed on your repository is
  the scanned patch, pushed to a fresh `agent/<job-id>` branch as a draft
  pull request. The GitHub App holds no `workflows` permission, so `.github/`
  cannot change even past the patch gate — and a human review stands between
  every change and a merge.
- **Nothing to leak.** There is no secrets store: a task cannot receive
  credentials, so a compromised task cannot expose any.
- **No new data path for your code.** Model calls from a task flow through
  the same FreeInference gateway and providers as your interactive API
  traffic.
- **Audit trail.** Every task keeps an append-only event log — each tool
  call and its result as it happened — and per-task model usage is metered
  on the platform side, independent of what the agent reports.

During the beta, task events and patches are retained with the job so you
can revisit past runs; a formal retention policy is planned.

## See also

- [Available Models](models.md) — full model catalog
- [IDE & Coding Agent Integrations](integrations.md) — run the same models
  interactively in your IDE
