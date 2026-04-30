# CF Pages doc.freeinference.org Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Point the existing Cloudflare Pages project at `doc.freeinference.org` to pull from `HarvardMadSys/hybridInference` instead of the old repo, and add `doc.staging.freeinference.org` as a `dev`-branch alias.

**Architecture:** The existing CF Pages project keeps its custom domain, hostname, and build config — only the git repo connection changes. A small `docs/free_inference/README.md` update documents the staging URL. All other steps are one-time Cloudflare dashboard actions. No new workflow files, no wrangler config, no new secrets.

**Tech Stack:** Cloudflare Pages (git integration), Sphinx (Python), uv

**Spec:** `docs/superpowers/specs/2026-04-29-cloudflare-workers-doc-deploy-design.md`

---

### Task 1: Set up worktree and branch

**Files:**
- No files created yet

- [ ] **Step 1: Pull latest dev**

```bash
git fetch origin dev
```

Expected: fetches without error.

- [ ] **Step 2: Create worktree**

```bash
git worktree add ../hybridInference-cf-pages-doc jason/claude/cf-pages-doc-deploy --track -b jason/claude/cf-pages-doc-deploy origin/dev 2>/dev/null || \
git worktree add ../hybridInference-cf-pages-doc -b jason/claude/cf-pages-doc-deploy origin/dev
cd ../hybridInference-cf-pages-doc
```

Expected: new directory `../hybridInference-cf-pages-doc` exists, `git branch` shows `jason/claude/cf-pages-doc-deploy`.

---

### Task 2: Verify the Sphinx build works locally (the smoke test)

This confirms what CF Pages will do when it builds the repo. If it fails locally it will fail in CF Pages.

**Files:**
- Read: `docs/free_inference/docs/source/conf.py`
- Read: `docs/free_inference/docs/Makefile`

- [ ] **Step 1: Install Python deps**

Run from the worktree root:

```bash
uv sync --group dev
```

Expected: resolves without error; `.venv/` populated.

- [ ] **Step 2: Build the docs**

```bash
cd docs/free_inference/docs
uv run sphinx-build -b html source build/html -W --keep-going
```

Expected: ends with `build succeeded` (or `build succeeded, N warning(s)`). The `-W` flag turns warnings into errors — if it fails here it will fail in CF Pages.

- [ ] **Step 3: Spot-check the output**

```bash
ls build/html/index.html build/html/quickstart.html build/html/models.html build/html/integrations.html
```

Expected: all four files exist.

- [ ] **Step 4: Clean up build output (do not commit it)**

```bash
rm -rf build/html
cd ../../..
```

---

### Task 3: Update docs/free_inference/README.md to document the staging URL

The README currently only mentions `doc.freeinference.org`. Add the staging hostname so future contributors know it exists.

**Files:**
- Modify: `docs/free_inference/README.md` (lines 11-12)

- [ ] **Step 1: Edit the Documentation section**

Open `docs/free_inference/README.md`. Find this block:

```markdown
## Documentation

Visit our documentation at: https://doc.freeinference.org/
```

Replace it with:

```markdown
## Documentation

- Production: https://doc.freeinference.org/
- Staging (tracks `dev` branch): https://doc.staging.freeinference.org/
```

- [ ] **Step 2: Verify the file looks right**

```bash
grep -A4 "## Documentation" docs/free_inference/README.md
```

Expected output:
```
## Documentation

- Production: https://doc.freeinference.org/
- Staging (tracks `dev` branch): https://doc.staging.freeinference.org/
```

- [ ] **Step 3: Update docs.yml comment to mention staging**

Open `.github/workflows/docs.yml`. Find this comment block (lines 13-16):

```yaml
# NOTE: Documentation is deployed via Cloudflare Pages:
#   - User docs: https://doc.freeinference.org/
#   - Developer docs: https://internaldoc.freeinference.org/
# Cloudflare automatically builds and deploys on push to main.
```

Replace with:

```yaml
# NOTE: Documentation is deployed via Cloudflare Pages:
#   - User docs (prod):    https://doc.freeinference.org/        (tracks main)
#   - User docs (staging): https://doc.staging.freeinference.org/ (tracks dev)
#   - Developer docs:      https://internaldoc.freeinference.org/ (tracks main)
# Cloudflare automatically builds and deploys on push to main / dev.
```

- [ ] **Step 4: Commit**

```bash
git add docs/free_inference/README.md .github/workflows/docs.yml
git commit -m "docs: document staging URL for user docs on CF Pages"
```

Expected: commit created on `jason/claude/cf-pages-doc-deploy`.

---

### Task 4: CF Pages dashboard — note current build config before changing anything

**This is a manual step. Do not skip it.** CF Pages may reset build settings when you change the connected repo. Record the current values before touching anything.

**Files:** None

- [ ] **Step 1: Open the Pages project**

Cloudflare dashboard → **Pages** → find the project currently serving `doc.freeinference.org`.

- [ ] **Step 2: Record current build settings**

Go to **Settings → Builds & Deployments**. Write down:

| Setting | Current value |
|---|---|
| Build command | e.g. `sphinx-build -b html source build/html` |
| Root directory | e.g. `docs/` or empty |
| Build output directory | e.g. `build/html` |
| Production branch | e.g. `main` |
| Python version (env var) | e.g. `PYTHON_VERSION=3.12` if set |

The expected correct values for this repo are:
- **Root directory:** `docs/free_inference/docs`
- **Build command:** `pip install sphinx sphinx-rtd-theme myst-parser && sphinx-build -b html source build/html -W --keep-going`
- **Build output directory:** `build/html`
- **Production branch:** `main`

---

### Task 5: CF Pages dashboard — reconnect repo

**This is a manual step.**

**Files:** None

- [ ] **Step 1: Disconnect the old repo**

In the Pages project → **Settings → Builds & Deployments → Git repository** → click **Manage** or **Disconnect**.

- [ ] **Step 2: Connect the new repo**

Select `HarvardMadSys/hybridInference`. Authorize CF Pages if prompted.

- [ ] **Step 3: Re-enter build settings**

After reconnecting, re-enter the values from Task 4 Step 2. CF Pages may have reset them:

| Setting | Value to enter |
|---|---|
| Root directory | `docs/free_inference/docs` |
| Build command | `pip install sphinx sphinx-rtd-theme myst-parser && sphinx-build -b html source build/html -W --keep-going` |
| Build output directory | `build/html` |
| Production branch | `main` |

Also set this **environment variable** (under **Settings → Environment Variables**) to pin Python 3.12 — CF Pages defaults to Python 3.7 which is too old for the packages:

| Variable | Value | Environment |
|---|---|---|
| `PYTHON_VERSION` | `3.12` | Production + Preview |

- [ ] **Step 4: Save**

Click **Save** / **Deploy**.

---

### Task 6: CF Pages dashboard — verify production build

**This is a manual step.**

**Files:** None

- [ ] **Step 1: Watch the build log**

CF Pages → project → **Deployments** tab. A new deployment should appear automatically after saving. Click it to watch the build log.

Expected: build ends with `Build complete` and shows `sphinx-build` output.

- [ ] **Step 2: Verify the live site**

Open `https://doc.freeinference.org/` in a browser.

Check:
- Homepage loads (title: "FreeInference Documentation")
- CSS renders (sidebar visible, RTD theme applied)
- Navigation links work (`/quickstart.html`, `/models.html`, `/integrations.html`)
- Search field is present

If the build fails, check the log for missing Python packages. The build command installs `sphinx sphinx-rtd-theme myst-parser` — all packages used by `docs/free_inference/docs/source/conf.py`. If `myst_parser` or `sphinx_rtd_theme` import errors appear, they are already covered. If any other package is missing, add it to the `pip install` command in Settings and retry.

---

### Task 7: CF Pages dashboard — add staging branch and hostname

**This is a manual step.**

**Files:** None

- [ ] **Step 1: Enable dev branch preview deployments**

CF Pages → project → **Settings → Builds & Deployments → Branch deploy controls**.

Under **Preview branches**, either:
- Select **All non-production branches**, or
- Select **Custom branches** and add `dev`.

- [ ] **Step 2: Add the staging custom domain**

CF Pages → project → **Custom Domains** → **Set up a custom domain** → enter `doc.staging.freeinference.org` → **Continue**.

When prompted to select a branch, choose `dev`.

Cloudflare will create the DNS record automatically (same account as the `freeinference.org` zone) and provision TLS. This may take up to 60 seconds.

- [ ] **Step 3: Trigger a staging build**

Push any commit to `dev` (or use the worktree's branch — after the PR is merged, push to `dev`). Alternatively, in CF Pages → **Deployments** → **Create deployment** → select `dev` branch.

- [ ] **Step 4: Verify the staging site**

Open `https://doc.staging.freeinference.org/` in a browser.

Check same items as Task 6 Step 2.

---

### Task 8: Cleanup old Pages project

**This is a manual step.**

**Files:** None

- [ ] **Step 1: Disable git integration on the old project**

In the Cloudflare dashboard, find the Pages project that was connected to the old repo (not the one you just reconnected — verify by checking its **Git repository** setting).

Go to **Settings → Builds & Deployments → Git repository** → **Disconnect**. This prevents it from auto-deploying if anyone pushes to the old repo.

Note: do not delete the project until you are confident `doc.freeinference.org` is fully healthy on the new project.

---

### Task 9: Create PR

**Files:**
- Modified: `docs/free_inference/README.md`
- Modified: `.github/workflows/docs.yml`

- [ ] **Step 1: Verify CI passes on the branch**

```bash
git log --oneline -5
```

Expected: shows the commit from Task 3 Step 4 at the top.

The `docs.yml` CI workflow will run on the PR and validate the Sphinx build. Wait for it to go green before merging.

- [ ] **Step 2: Push branch**

```bash
git push -u origin jason/claude/cf-pages-doc-deploy
```

- [ ] **Step 3: Create PR**

```bash
gh pr create \
  --title "docs: migrate doc.freeinference.org CF Pages to this repo" \
  --base dev \
  --body "$(cat <<'EOF'
## Summary

- Reconnects the existing Cloudflare Pages project for `doc.freeinference.org` from the old standalone repo to this monorepo, where the Sphinx source now lives (`docs/free_inference/`).
- Documents the staging URL (`doc.staging.freeinference.org`) in the README and `docs.yml` comment.
- No new workflow files, no wrangler config, no new secrets — the CF Pages git integration handles deploys.

## CF Pages manual steps

The actual CF dashboard migration steps are documented in:
`docs/superpowers/specs/2026-04-29-cloudflare-workers-doc-deploy-design.md`

Run Tasks 4–8 of the implementation plan after this PR merges.

## Test plan

- [ ] `docs.yml` CI passes on this PR (Sphinx build validates without error)
- [ ] After CF Pages reconnect: `https://doc.freeinference.org/` loads correctly
- [ ] After staging setup: `https://doc.staging.freeinference.org/` loads correctly

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed. Share it.
