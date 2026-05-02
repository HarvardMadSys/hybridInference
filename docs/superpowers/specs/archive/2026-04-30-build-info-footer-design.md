# Build Info in Footer

**Date:** 2026-04-30
**Status:** Approved

## Goal

Show the deployed commit SHA and deployment timestamp in the frontend so we can quickly tell what's running in production / staging at a glance.

## What & Where

- Render in the global footer in [`frontend/src/app/layout.tsx`](../../../frontend/src/app/layout.tsx). The footer already appears on every page (login, dashboard, etc.); the root `/` page just redirects, so the footer is the right surface.
- Display short SHA (7 chars) as a clickable link to `https://github.com/HarvardMadSys/hybridInference/commit/<full-sha>` and the deployment timestamp formatted in the user's local timezone.
- Example footer line:
  `© FreeInference · build a1b2c3d · deployed 2026-04-30 14:23 UTC`
- Fallback when env vars are unset (local `npm run dev`): show `build dev · local build`.

## How It's Wired

1. **Build args** — add to [`infrastructure/docker/Dockerfile.frontend`](../../../infrastructure/docker/Dockerfile.frontend):
   ```dockerfile
   ARG NEXT_PUBLIC_BUILD_SHA=
   ARG NEXT_PUBLIC_BUILD_TIMESTAMP=
   ENV NEXT_PUBLIC_BUILD_SHA=$NEXT_PUBLIC_BUILD_SHA
   ENV NEXT_PUBLIC_BUILD_TIMESTAMP=$NEXT_PUBLIC_BUILD_TIMESTAMP
   ```
2. **Compose plumbing** — forward env to build args in [`infrastructure/docker/docker-compose.yml`](../../../infrastructure/docker/docker-compose.yml) frontend service:
   ```yaml
   args:
     NEXT_PUBLIC_BUILD_SHA: ${BUILD_SHA:-}
     NEXT_PUBLIC_BUILD_TIMESTAMP: ${BUILD_TIMESTAMP:-}
   ```
   Apply the same change to [`docker-compose.staging.yml`](../../../infrastructure/docker/docker-compose.staging.yml) if it has its own frontend build block.
3. **Deploy scripts** — in both [`scripts/deploy_production.sh`](../../../scripts/deploy_production.sh) and [`scripts/deploy_staging.sh`](../../../scripts/deploy_staging.sh), export before `make build`:
   ```bash
   export BUILD_SHA="$target_sha"
   export BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```
4. **Config exposure** — add to [`frontend/src/config/env.ts`](../../../frontend/src/config/env.ts):
   ```ts
   buildSha: process.env.NEXT_PUBLIC_BUILD_SHA || '',
   buildTimestamp: process.env.NEXT_PUBLIC_BUILD_TIMESTAMP || '',
   ```
5. **Footer rendering** — small inline component in `layout.tsx` (or a tiny `BuildInfo.tsx` under `components/ui/`). Logic:
   - If `buildSha` empty → render `build dev`.
   - Else: render `build <short>` as `<a href="https://github.com/HarvardMadSys/hybridInference/commit/<full>" target="_blank" rel="noreferrer">…</a>`.
   - If `buildTimestamp` empty → `local build`.
   - Else: parse the ISO timestamp and render as `deployed YYYY-MM-DD HH:MM UTC`. Use a `<time dateTime={…}>` element with the ISO value as the machine-readable form so accessibility/timezone tooling works.

## Non-Goals

- No backend `/api/version` endpoint. (The frontend is the user-facing surface; the backend SHA is the same `target_sha` since both deploy from the same repo state.)
- No build dirty/branch metadata. SHA + timestamp is enough.

## Verification

- Run `npm run dev` locally → footer shows `build dev · local build`.
- Build with `--build-arg NEXT_PUBLIC_BUILD_SHA=abc1234 --build-arg NEXT_PUBLIC_BUILD_TIMESTAMP=2026-04-30T14:23:00Z` and confirm footer renders the linked SHA and formatted timestamp.
- Run `make frontend-check` (lint + type-check + tests) and `make lint` (Python linters for the deploy scripts? — they're shell, but `ruff format --check` should still pass since no Python touched).
