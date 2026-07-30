# Cloud Agents

Describe a task, and FreeInference checks out your GitHub repository, runs a
coding agent on it in an isolated cloud sandbox, and opens a **draft pull
request** with the result. Each task picks which agent runtime does the work
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
3. Pick the **Runtime** and the **Model**
4. Run the task

The task page streams the agent's activity as it happens — its reasoning, each
tool call with its result, and the diff as it takes shape. A running task can
be cancelled at any point. When the task finishes, the draft pull request is
linked directly from the task page.

## Choosing a runtime and model

The **runtime** is the coding agent that does the work; the **model** is what
drives it. Both are picked per task, and any listed model can drive any
runtime:

- **Runtime** — Claude Code and Codex are available today.
- **Model** — the composer lists the models enabled for agent tasks, which
  during the beta can be a subset of the full [model catalog](models.md).

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

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Task sits in "Queued" | Waiting for runner capacity during the beta | It starts when capacity frees up — or cancel and retry later |
| Task finished but there is no pull request | The run produced no patch, for example because tool calls failed | Open the task page's activity feed — failed tool calls are shown there, and a run with tool failures and no patch is reported as failed, not dressed up as success |
| A model you use elsewhere is missing from the composer | Only models enabled for agent tasks are listed during the beta | Pick from the listed models |
| A repository is missing from the composer | The GitHub App installation does not cover it | Edit the App's repository access in your GitHub settings |

## See also

- [Available Models](models.md) — full model catalog
- [IDE & Coding Agent Integrations](integrations.md) — run the same models
  interactively in your IDE
