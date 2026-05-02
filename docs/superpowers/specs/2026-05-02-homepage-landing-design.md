# Public Homepage Landing Page — Design

**Date:** 2026-05-02
**Status:** Draft
**Owner:** jason

## Summary

Replace the current redirect-only homepage at `/` with a public landing page that introduces freeinference.org, a free LLM inference gateway built at Harvard SEAS. Authenticated users continue to redirect to `/dashboard`; unauthenticated visitors now see informational content and CTAs to sign up or log in (instead of being immediately bounced to `/login`).

## Goals

- Give first-time visitors a clear value proposition without forcing them to a login form.
- Brand the project as a Harvard SEAS effort using crimson colors and serif typography.
- Keep the page static and self-contained: no new API endpoints, no new server logic.
- Reuse existing UI primitives (`Button`, `Card`) and layout shell.

## Non-Goals

- No marketing CMS, blog, or i18n.
- No model catalog page, no live model status, no pricing tiers.
- No redesign of dashboard, login, or signup pages.
- No new backend or API surface.

## Current State

[frontend/src/app/page.tsx](../../../frontend/src/app/page.tsx) currently redirects:

- Authenticated → `/dashboard`
- Unauthenticated → `/login`

The page renders a spinner while redirecting and never shows landing content.

The shared layout in [frontend/src/app/layout.tsx](../../../frontend/src/app/layout.tsx) provides a centered `max-w-5xl` container with a header (app name) and a footer (build info). All pages share this shell.

## Design

### Routing & Auth Behavior

In [frontend/src/app/page.tsx](../../../frontend/src/app/page.tsx):

- Keep the page as a client component (`'use client'`) because it consumes `useAuth`.
- If `state.loading` → render a small loading placeholder (or just the page; loading is brief).
- If `state.isAuthenticated` → `router.replace('/dashboard')` and render nothing.
- Otherwise → render the landing layout (Hero, Features, HowItWorks, CodeExample).

The `/login` and `/signup` pages remain unchanged. Header/footer remain shared via the root layout.

### Visual Style

- **Primary color:** Harvard Crimson `#A51C30`.
- **Hover/dark:** `#8B1729`.
- **Headings font:** Crimson Text (loaded via `next/font/google` in [frontend/src/app/layout.tsx](../../../frontend/src/app/layout.tsx)), exposed via a CSS variable (e.g. `--font-serif`) and applied with a Tailwind utility class on h1/h2.
- **Body font:** existing default sans (no change).
- **Hero background:** subtle gradient `bg-gradient-to-br from-white via-gray-50 to-red-50/30`. Other sections use the existing `bg-gray-50`.
- The shared header gains a small "Harvard SEAS" subtitle next to the app name (serif, gray-500). The shared footer adds links to docs and GitHub plus an SEAS attribution line.

Tailwind tokens for crimson can be added to `tailwind.config.*` (or used as arbitrary `text-[#A51C30]` values if config edits are out of scope). Prefer adding a `crimson` color to the Tailwind theme so usages stay readable.

### New Components

Create [frontend/src/components/landing/](../../../frontend/src/components/landing/) with four files. Each is a small, focused presentational component with no internal state.

#### `Hero.tsx`

- H1 (serif): **"Free LLM Inference for Research"**
- Sub (sans, gray-600): "OpenAI-compatible API powered by frontier open and proprietary models. Built at Harvard SEAS."
- Two CTAs:
  - `Sign up free` → `/signup` (crimson solid button)
  - `Sign in` → `/login` (outline button)
- Vertical padding: large (e.g. `py-20`).

#### `Features.tsx`

3-column responsive grid (`grid md:grid-cols-3 gap-6`) wrapping 5 `Card` items. Layout collapses to single column on mobile and 2 columns on `md` if the 3-col layout looks awkward — designer's call during implementation.

| Feature | Headline | Body |
|---|---|---|
| Free Access | **Free to use** | No credit card. Generous quota for research and prototyping. |
| OpenAI-Compatible API | **Drop-in replacement** | Point your existing OpenAI client at our `base_url`. No code changes. |
| Frontier Models | **Top open and proprietary models** | GLM, Minimax, Qwen, and Anthropic — all behind one API. |
| Streaming & Tool Calls | **Full feature parity** | Server-sent streaming, tool calls, and structured output supported. |
| Usage Dashboard | **Live usage and keys** | Track token usage, manage API keys, monitor quotas. |

Icons are optional; if added, use a single icon library already in the project (check `frontend/package.json` during implementation). Otherwise, render headlines without icons.

#### `HowItWorks.tsx`

3 numbered steps in a horizontal layout (collapses to vertical on mobile):

1. **Sign up** — Create a free account with your email.
2. **Create an API key** — Generate a key from your dashboard.
3. **Call the API** — Use any OpenAI-compatible client.

Each step has a crimson numbered circle, a serif headline, and a one-line description.

#### `CodeExample.tsx`

A single dark-themed `<pre>` block showing a curl example against the gateway. Include a copy button (button shows "Copy" → "Copied!" for 2s). Example:

```bash
curl https://freeinference.org/v1/chat/completions \
  -H "Authorization: Bearer $FREEINFERENCE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-4.7",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

YAGNI: ship curl-only. No language tabs in v1. The component is structured so a future tab UI can wrap the `<pre>` if requested later.

### Layout Changes

In [frontend/src/app/layout.tsx](../../../frontend/src/app/layout.tsx):

- Load Crimson Text via `next/font/google`, expose as a CSS variable.
- Apply the variable on `<html>` so children can opt in via Tailwind utility (e.g. `font-serif`).
- Header: add a small SEAS subtitle next to the app name (e.g. `Harvard SEAS`, `text-sm text-gray-500 font-serif`).
- Footer: extend the existing line to include:
  `© {appName} · Harvard SEAS · Docs · GitHub · BuildInfo`
  Links go to `https://doc.freeinference.org` and `https://github.com/HarvardSys/hybridInference`.

The `<main>` wrapper currently has `items-center` and `py-12`. The landing page wants top-aligned, full-width sections. Two options for handling this:

- **Option A (preferred):** widen the `<main>` to allow full-width content via `w-full` and let each landing section manage its own internal `max-w-5xl mx-auto`. Existing centered pages (login, signup) keep the same visual result because their card contents are already constrained.
- **Option B:** add a per-route layout file at [frontend/src/app/(landing)/layout.tsx](../../../frontend/src/app/(landing)/layout.tsx) that overrides only the landing route group.

Pick A unless it visually breaks login/signup. The implementation plan should verify this with a quick before/after check on those pages.

### Data Flow

Static content. No new API calls, no new state. The only runtime concern is the auth check (`useAuth`) used to redirect authenticated users to the dashboard.

### Error Handling

No error paths beyond what `useAuth` already handles. If auth state fails to load, the existing `ErrorBoundary` in `layout.tsx` covers rendering errors.

## Testing

- **Manual flows:**
  - Visit `/` while unauthenticated → see landing content, no redirect.
  - Click `Sign up free` → land on `/signup`.
  - Click `Sign in` → land on `/login`.
  - Log in, then visit `/` → redirected to `/dashboard`.
  - Hard-refresh `/` while authenticated → redirected to `/dashboard`.
  - Mobile viewport (≤640px) → sections stack, no horizontal scroll.
- **Code example copy button** — clicking copies the curl block to clipboard and shows a brief "Copied!" affordance.
- **Lint/format gate (per CLAUDE.md):** `uv run ruff format --check .` before opening PR. Frontend lint via existing project scripts (e.g., `npm run lint`) if defined.
- **Visual verification on staging:** deploy to https://staging.freeinference.org and verify the landing page renders correctly for both authenticated and anonymous sessions (anonymous: incognito window; authenticated: log in with `admin@admin.com` / `admin`).

## Files Touched

- Modify: [frontend/src/app/page.tsx](../../../frontend/src/app/page.tsx) — replace redirect-only logic with landing render.
- Modify: [frontend/src/app/layout.tsx](../../../frontend/src/app/layout.tsx) — load serif font, extend footer, add SEAS subtitle.
- Modify: `frontend/tailwind.config.*` — add `crimson` color token (if config exists; otherwise use arbitrary values).
- New: `frontend/src/components/landing/Hero.tsx`
- New: `frontend/src/components/landing/Features.tsx`
- New: `frontend/src/components/landing/HowItWorks.tsx`
- New: `frontend/src/components/landing/CodeExample.tsx`

## Open Questions

None blocking. Implementation may revisit:

- Whether to add icons to feature cards (depends on installed libraries).
- Whether main wrapper widening (option A) impacts existing centered pages.

## Out-of-Scope Follow-Ups

- Light/dark theme toggle.
- A separate `/models` catalog page.
- SEO metadata polish (Open Graph, Twitter cards) beyond the existing `<title>`/`<description>`.
- Internationalization.
