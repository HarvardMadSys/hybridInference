# Public Homepage Landing Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the redirect-only `/` route with a public landing page that introduces freeinference.org as a Harvard SEAS project, while preserving the auth-based redirect to `/dashboard` for logged-in users.

**Architecture:** Frontend-only change in the Next.js app. Add Harvard Crimson colors and Crimson Text serif headings to the existing Tailwind theme, extend the shared layout with SEAS attribution and a serif font variable, and create four small presentational components in `frontend/src/components/landing/` (Hero, Features, HowItWorks, CodeExample). The homepage at [frontend/src/app/page.tsx](../../../frontend/src/app/page.tsx) renders these for unauthenticated visitors and redirects authenticated visitors to `/dashboard`.

**Tech Stack:** Next.js 15 (App Router, client components), React 18, Tailwind CSS 3, TypeScript, `next/font/google`, existing `Button` and `Card` UI primitives, Vitest for unit tests where applicable.

**Spec:** [docs/superpowers/specs/2026-05-02-homepage-landing-design.md](../specs/2026-05-02-homepage-landing-design.md)

---

## File Structure

**Create:**
- `frontend/src/components/landing/Hero.tsx` — hero section with headline, subhead, two CTAs.
- `frontend/src/components/landing/Features.tsx` — 5-card responsive grid of feature highlights.
- `frontend/src/components/landing/HowItWorks.tsx` — 3-step numbered onboarding visual.
- `frontend/src/components/landing/CodeExample.tsx` — dark `<pre>` block with curl example and copy button.
- `frontend/src/components/landing/index.ts` — re-exports for ergonomic imports from the page.

**Modify:**
- `frontend/tailwind.config.ts` — add `crimson` color palette and `serif` font family pointing to a CSS variable.
- `frontend/src/app/layout.tsx` — load Crimson Text via `next/font/google`, expose CSS variable on `<html>`, add SEAS subtitle to header, extend footer with SEAS attribution and links.
- `frontend/src/app/page.tsx` — render landing components for unauthenticated visitors; keep auth → `/dashboard` redirect.

No backend changes. No new dependencies (Crimson Text comes from `next/font/google`, already implicitly available).

---

## Pre-Flight

- [ ] **Step 0a: Pull latest dev**

```bash
git fetch origin
git checkout dev
git pull origin dev
```

Expected: working tree clean, branch up to date with `origin/dev`.

- [ ] **Step 0b: Create worktree and feature branch (per CLAUDE.md)**

```bash
git worktree add ../hybridInference-homepage-landing -b jason/claude/homepage-landing dev
cd ../hybridInference-homepage-landing
```

Expected: new worktree at `../hybridInference-homepage-landing`, branch `jason/claude/homepage-landing` checked out.

All subsequent steps run inside the worktree.

- [ ] **Step 0c: Install frontend dependencies**

```bash
cd frontend
npm install
```

Expected: `node_modules/` populated, no errors.

---

## Task 1: Add Crimson Color and Serif Font to Tailwind Theme

**Files:**
- Modify: `frontend/tailwind.config.ts`

- [ ] **Step 1.1: Add crimson palette and serif font family**

Replace the existing `theme.extend` block in `frontend/tailwind.config.ts` with:

```ts
import type { Config } from 'tailwindcss';

export default {
  content: ['./src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        brand: {
          primary: '#111827',
          accent: '#3B82F6',
          bgLight: '#F9FAFB',
          bgDark: '#0B1220',
        },
        crimson: {
          DEFAULT: '#A51C30',
          dark: '#8B1729',
          light: '#C8324A',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        serif: ['var(--font-serif)', 'Georgia', 'Cambria', 'Times New Roman', 'serif'],
      },
      borderRadius: {
        lg: '0.5rem',
      },
      boxShadow: {
        subtle: '0 1px 2px rgba(0,0,0,0.06)',
        card: '0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)',
      },
    },
  },
  plugins: [],
} satisfies Config;
```

- [ ] **Step 1.2: Verify Tailwind picks up the new tokens**

Run from `frontend/`:

```bash
npm run type-check
```

Expected: PASS (no TS errors). Tailwind config is type-checked along with the rest.

- [ ] **Step 1.3: Commit**

```bash
git add frontend/tailwind.config.ts
git commit -m "feat(frontend): add crimson color palette and serif font family"
```

---

## Task 2: Load Crimson Text Serif and Extend Layout Header/Footer

**Files:**
- Modify: `frontend/src/app/layout.tsx`

- [ ] **Step 2.1: Update `layout.tsx` to load the serif font and extend header/footer**

Replace the contents of `frontend/src/app/layout.tsx` with:

```tsx
import '../styles/globals.css';
import Script from 'next/script';
import { Crimson_Text } from 'next/font/google';
import { config } from '@/config/env';
import { Providers } from '@/components/providers';
import { ErrorBoundary } from '@/components/ui/ErrorBoundary';
import { BuildInfo } from '@/components/ui/BuildInfo';

const crimsonText = Crimson_Text({
  subsets: ['latin'],
  weight: ['400', '600', '700'],
  variable: '--font-serif',
  display: 'swap',
});

export const metadata = {
  title: config.appName,
  description: 'Free LLM inference for research, built at Harvard SEAS.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`h-full ${crimsonText.variable}`}>
      <head>
        <Script id="statcounter-config" strategy="afterInteractive">
          {"var sc_project=13224568; var sc_invisible=1; var sc_security='2d8ab84a';"}
        </Script>
        <Script
          id="statcounter-loader"
          src="https://www.statcounter.com/counter/counter.js"
          strategy="afterInteractive"
        />
      </head>
      <body
        className="flex min-h-screen flex-col bg-gray-50 text-black antialiased"
        suppressHydrationWarning
      >
        <ErrorBoundary>
          <Providers>
            <header className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-6">
              <div className="flex items-baseline gap-2">
                <span className="text-xl font-bold tracking-tight">{config.appName}</span>
                <span className="font-serif text-sm text-gray-500">Harvard SEAS</span>
              </div>
            </header>
            <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-6 py-12">
              {children}
            </main>
            <footer className="mx-auto w-full max-w-5xl px-6 py-6 text-center text-sm text-gray-400">
              <div className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1">
                <span>© {config.appName}</span>
                <span aria-hidden="true">·</span>
                <span>Harvard SEAS</span>
                <span aria-hidden="true">·</span>
                <a
                  href="https://doc.freeinference.org"
                  className="hover:text-crimson"
                  target="_blank"
                  rel="noreferrer"
                >
                  Docs
                </a>
                <span aria-hidden="true">·</span>
                <a
                  href="https://github.com/HarvardSys/hybridInference"
                  className="hover:text-crimson"
                  target="_blank"
                  rel="noreferrer"
                >
                  GitHub
                </a>
                <span aria-hidden="true">·</span>
                <BuildInfo />
              </div>
            </footer>
            <noscript>
              <div className="statcounter">
                <a
                  title="Web Analytics"
                  href="https://statcounter.com/"
                  target="_blank"
                  rel="noreferrer"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    className="statcounter"
                    src="https://c.statcounter.com/13224568/0/2d8ab84a/1/"
                    alt="Web Analytics"
                    referrerPolicy="no-referrer-when-downgrade"
                  />
                </a>
              </div>
            </noscript>
          </Providers>
        </ErrorBoundary>
      </body>
    </html>
  );
}
```

Notes for the implementer:
- `<main>` lost `items-center`. Existing pages (`/login`, `/signup`) place their own `Card` inside `<main>` and rely on the parent flex centering. After this change, those pages will be top-aligned within `<main>`. We accept this for the landing page; visually verify in Step 2.3 that login/signup still look fine. If they do not, add `items-center` back and instead override `items-stretch` on the landing page wrapper itself.

- [ ] **Step 2.2: Type-check and lint**

```bash
cd frontend
npm run type-check
npm run lint
```

Expected: both PASS with no new warnings.

- [ ] **Step 2.3: Visual smoke test for login/signup pages**

```bash
npm run dev
```

Open http://localhost:3001/login and http://localhost:3001/signup in a browser. Confirm the login and signup cards still render correctly (centered horizontally; vertical placement is now near the top of `<main>`, which is acceptable). If centering is broken in a way that looks wrong, revert `<main>`'s class to keep `items-center` and instead add `items-stretch` on the landing-page wrapper component. Stop the dev server with Ctrl+C.

- [ ] **Step 2.4: Commit**

```bash
git add frontend/src/app/layout.tsx
git commit -m "feat(frontend): load Crimson Text serif, add SEAS header subtitle and footer links"
```

---

## Task 3: Build the Hero Component

**Files:**
- Create: `frontend/src/components/landing/Hero.tsx`

- [ ] **Step 3.1: Implement `Hero.tsx`**

```tsx
import Link from 'next/link';

export function Hero(): JSX.Element {
  return (
    <section className="relative w-full overflow-hidden rounded-2xl bg-gradient-to-br from-white via-gray-50 to-red-50/40 px-6 py-20 text-center shadow-subtle">
      <h1 className="mx-auto max-w-3xl font-serif text-4xl font-bold tracking-tight text-gray-900 sm:text-5xl md:text-6xl">
        Free LLM Inference <span className="text-crimson">for Research</span>
      </h1>
      <p className="mx-auto mt-6 max-w-2xl text-base text-gray-600 sm:text-lg">
        OpenAI-compatible API powered by frontier open and proprietary models. Built at Harvard SEAS.
      </p>
      <div className="mt-10 flex flex-col items-center justify-center gap-3 sm:flex-row">
        <Link
          href="/signup"
          className="inline-flex h-11 items-center justify-center rounded-md bg-crimson px-6 text-base font-medium text-white shadow-sm transition-colors duration-200 hover:bg-crimson-dark focus:outline-none focus:ring-2 focus:ring-crimson focus:ring-offset-2"
        >
          Sign up free
        </Link>
        <Link
          href="/login"
          className="inline-flex h-11 items-center justify-center rounded-md border border-gray-300 bg-white px-6 text-base font-medium text-gray-900 shadow-sm transition-colors duration-200 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2"
        >
          Sign in
        </Link>
      </div>
    </section>
  );
}
```

- [ ] **Step 3.2: Type-check**

```bash
cd frontend
npm run type-check
```

Expected: PASS.

- [ ] **Step 3.3: Commit**

```bash
git add frontend/src/components/landing/Hero.tsx
git commit -m "feat(frontend): add landing Hero component"
```

---

## Task 4: Build the Features Component

**Files:**
- Create: `frontend/src/components/landing/Features.tsx`

- [ ] **Step 4.1: Implement `Features.tsx`**

```tsx
interface Feature {
  title: string;
  body: string;
}

const FEATURES: Feature[] = [
  {
    title: 'Free to use',
    body: 'No credit card required. Generous quota for research and prototyping.',
  },
  {
    title: 'Drop-in OpenAI replacement',
    body: 'Point your existing OpenAI client at our base_url. No code changes required.',
  },
  {
    title: 'Frontier models',
    body: 'GLM, Minimax, Qwen, and Anthropic models — all behind a single unified API.',
  },
  {
    title: 'Streaming and tool calls',
    body: 'Server-sent streaming, tool calls, and structured output supported end-to-end.',
  },
  {
    title: 'Live usage and keys',
    body: 'Track token usage, manage API keys, and monitor quotas from your dashboard.',
  },
];

export function Features(): JSX.Element {
  return (
    <section className="w-full py-16">
      <div className="mx-auto max-w-3xl text-center">
        <h2 className="font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          Why <span className="text-crimson">freeinference.org</span>
        </h2>
        <p className="mt-3 text-base text-gray-600">
          Everything you need to build and ship LLM-powered applications.
        </p>
      </div>

      <div className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map((feature) => (
          <div
            key={feature.title}
            className="rounded-xl border border-gray-200 bg-white p-6 shadow-subtle transition-shadow duration-200 hover:shadow-card"
          >
            <h3 className="font-serif text-lg font-semibold text-gray-900">{feature.title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-gray-600">{feature.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
```

- [ ] **Step 4.2: Type-check**

```bash
cd frontend
npm run type-check
```

Expected: PASS.

- [ ] **Step 4.3: Commit**

```bash
git add frontend/src/components/landing/Features.tsx
git commit -m "feat(frontend): add landing Features component"
```

---

## Task 5: Build the HowItWorks Component

**Files:**
- Create: `frontend/src/components/landing/HowItWorks.tsx`

- [ ] **Step 5.1: Implement `HowItWorks.tsx`**

```tsx
interface Step {
  number: string;
  title: string;
  body: string;
}

const STEPS: Step[] = [
  {
    number: '1',
    title: 'Sign up',
    body: 'Create a free account with your email — no credit card needed.',
  },
  {
    number: '2',
    title: 'Create an API key',
    body: 'Generate a key from your dashboard in one click.',
  },
  {
    number: '3',
    title: 'Call the API',
    body: 'Use any OpenAI-compatible client. Just change the base URL.',
  },
];

export function HowItWorks(): JSX.Element {
  return (
    <section className="w-full py-16">
      <div className="mx-auto max-w-3xl text-center">
        <h2 className="font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          Get started in three steps
        </h2>
      </div>

      <ol className="mt-10 grid gap-8 md:grid-cols-3">
        {STEPS.map((step) => (
          <li key={step.number} className="flex flex-col items-center text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-crimson font-serif text-xl font-bold text-white shadow-sm">
              {step.number}
            </span>
            <h3 className="mt-4 font-serif text-xl font-semibold text-gray-900">{step.title}</h3>
            <p className="mt-2 max-w-xs text-sm leading-relaxed text-gray-600">{step.body}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}
```

- [ ] **Step 5.2: Type-check**

```bash
cd frontend
npm run type-check
```

Expected: PASS.

- [ ] **Step 5.3: Commit**

```bash
git add frontend/src/components/landing/HowItWorks.tsx
git commit -m "feat(frontend): add landing HowItWorks component"
```

---

## Task 6: Build the CodeExample Component

**Files:**
- Create: `frontend/src/components/landing/CodeExample.tsx`

- [ ] **Step 6.1: Implement `CodeExample.tsx`**

```tsx
'use client';

import { useState } from 'react';

const CURL_EXAMPLE = `curl https://freeinference.org/v1/chat/completions \\
  -H "Authorization: Bearer $FREEINFERENCE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "glm-4.7",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'`;

export function CodeExample(): JSX.Element {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(CURL_EXAMPLE);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable; do nothing.
    }
  }

  return (
    <section className="w-full py-16">
      <div className="mx-auto max-w-3xl text-center">
        <h2 className="font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          One <code className="font-mono text-crimson">curl</code> away
        </h2>
        <p className="mt-3 text-base text-gray-600">
          Use the same OpenAI client libraries you already know.
        </p>
      </div>

      <div className="relative mt-8 overflow-hidden rounded-xl bg-gray-900 shadow-card">
        <div className="flex items-center justify-between border-b border-gray-800 px-4 py-2">
          <span className="font-mono text-xs uppercase tracking-wider text-gray-400">bash</span>
          <button
            type="button"
            onClick={handleCopy}
            className="rounded-md border border-gray-700 px-3 py-1 text-xs font-medium text-gray-300 transition-colors duration-150 hover:bg-gray-800"
            aria-label="Copy code"
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
        </div>
        <pre className="overflow-x-auto px-4 py-4 text-sm leading-relaxed text-gray-100">
          <code>{CURL_EXAMPLE}</code>
        </pre>
      </div>
    </section>
  );
}
```

- [ ] **Step 6.2: Type-check**

```bash
cd frontend
npm run type-check
```

Expected: PASS.

- [ ] **Step 6.3: Commit**

```bash
git add frontend/src/components/landing/CodeExample.tsx
git commit -m "feat(frontend): add landing CodeExample component with copy button"
```

---

## Task 7: Add Index Re-Export

**Files:**
- Create: `frontend/src/components/landing/index.ts`

- [ ] **Step 7.1: Implement `index.ts`**

```ts
export { Hero } from './Hero';
export { Features } from './Features';
export { HowItWorks } from './HowItWorks';
export { CodeExample } from './CodeExample';
```

- [ ] **Step 7.2: Commit**

```bash
git add frontend/src/components/landing/index.ts
git commit -m "feat(frontend): add landing components index re-export"
```

---

## Task 8: Replace Homepage to Render Landing for Unauthenticated Visitors

**Files:**
- Modify: `frontend/src/app/page.tsx`

- [ ] **Step 8.1: Replace the contents of `frontend/src/app/page.tsx`**

```tsx
'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/components/providers';
import {
  CodeExample,
  Features,
  Hero,
  HowItWorks,
} from '@/components/landing';

export default function HomePage(): JSX.Element {
  const router = useRouter();
  const { state } = useAuth();

  useEffect(() => {
    if (state.loading) return;
    if (state.isAuthenticated) {
      router.replace('/dashboard');
    }
  }, [state.loading, state.isAuthenticated, router]);

  if (state.loading || state.isAuthenticated) {
    return (
      <div className="flex w-full items-center justify-center">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-crimson"></div>
      </div>
    );
  }

  return (
    <div className="flex w-full flex-col gap-4">
      <Hero />
      <Features />
      <HowItWorks />
      <CodeExample />
    </div>
  );
}
```

Behavior:
- While auth state loads or while authenticated → spinner (then redirect for authenticated).
- Otherwise → render the four landing sections.

- [ ] **Step 8.2: Type-check and lint**

```bash
cd frontend
npm run type-check
npm run lint
```

Expected: both PASS, no new warnings.

- [ ] **Step 8.3: Commit**

```bash
git add frontend/src/app/page.tsx
git commit -m "feat(frontend): render public landing at / for unauthenticated visitors"
```

---

## Task 9: End-to-End Manual Verification

**Files:** none modified.

- [ ] **Step 9.1: Build and start the frontend**

```bash
cd frontend
npm run build
npm run start
```

Expected: build succeeds; app starts on port 3001 (or whatever port `next start` selects — usually 3000; if it does not match the dev port, that is fine for static verification).

If a backend is required for `/dashboard`, just verify the landing render and the redirect logic — no need to fully boot the API.

- [ ] **Step 9.2: Anonymous flow check**

In an incognito window:

1. Visit http://localhost:3001/ → see Hero, Features, HowItWorks, CodeExample. No redirect.
2. Click `Sign up free` → land on `/signup`.
3. Back to `/` → click `Sign in` → land on `/login`.
4. Click the "Copy" button on the curl block → it shows "Copied!" briefly and the clipboard now contains the curl snippet (paste into a text field to verify).
5. Resize browser to ~375px wide → sections stack, no horizontal scroll, CTAs stack vertically.

- [ ] **Step 9.3: Authenticated flow check**

In a non-incognito window with an active staging session (or by logging in locally via `admin@admin.com` / `admin` if local backend is up):

1. Visit `/` → spinner briefly, then redirect to `/dashboard`. Landing sections never appear.
2. Hard-refresh `/dashboard` → still on dashboard.

- [ ] **Step 9.4: Stop the server**

Ctrl+C in the terminal running `npm run start`.

- [ ] **Step 9.5: Commit any tweaks**

If Step 9.2 or 9.3 surfaced minor issues (typo, spacing, broken link), fix inline and commit:

```bash
git add <files>
git commit -m "fix(frontend): polish landing based on manual QA"
```

---

## Task 10: Pre-PR Quality Gate

- [ ] **Step 10.1: Run Python lint (per CLAUDE.md)**

From the repo root of the worktree:

```bash
uv run ruff format --check .
```

Expected: PASS. (No Python files were modified, so this should be a no-op pass.)

- [ ] **Step 10.2: Run frontend formatters and lint**

```bash
cd frontend
npm run format:check
npm run lint
npm run type-check
npm run build
```

Expected: all four PASS. If `format:check` fails because of new files, run `npm run format` and re-run `format:check`, then commit:

```bash
git add -u
git commit -m "chore(frontend): apply prettier formatting"
```

- [ ] **Step 10.3: Verify branch state**

```bash
git status
git log dev..HEAD --oneline
```

Expected: clean working tree; all expected commits listed (one per task plus any polish commits).

---

## Task 11: Open PR to dev

- [ ] **Step 11.1: Push branch**

```bash
git push -u origin jason/claude/homepage-landing
```

- [ ] **Step 11.2: Create PR**

```bash
gh pr create --base dev --title "feat(frontend): public homepage landing page with Harvard SEAS branding" --body "$(cat <<'EOF'
## Summary
- Replace redirect-only `/` with a public landing page (Hero, Features, HowItWorks, CodeExample) for unauthenticated visitors.
- Brand the project as Harvard SEAS using Harvard Crimson `#A51C30` and Crimson Text serif headings.
- Authenticated users continue to redirect to `/dashboard`.

## Spec
docs/superpowers/specs/2026-05-02-homepage-landing-design.md

## Test plan
- [ ] Anonymous visit to `/` renders all four sections, no redirect
- [ ] `Sign up free` and `Sign in` CTAs navigate correctly
- [ ] Copy button on curl example copies to clipboard and toggles label briefly
- [ ] Authenticated visit to `/` redirects to `/dashboard`
- [ ] Mobile viewport (~375px) renders without horizontal scroll
- [ ] Login and signup pages still render correctly under the modified `<main>` layout
- [ ] Verify on https://staging.freeinference.org after merge

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed. Save the URL for the user.

- [ ] **Step 11.3: Address any CI / review comments**

Per CLAUDE.md: do NOT merge until all comments are addressed and CI passes. Continue iterating with new commits on `jason/claude/homepage-landing` and pushing. Reply to inline comments only after fixing.

---

## Test Strategy Note

This codebase has Vitest configured for utility/schema tests but does **not** have React Testing Library installed. Adding RTL just to assert "the Hero renders an h1 with text X" is scope creep relative to the design value of the assertion, and the components are pure presentational JSX with no logic branches except the copy-button toggle in `CodeExample`. The plan therefore relies on:

1. TypeScript type-checking (catches structural errors).
2. ESLint (catches React and accessibility issues).
3. Next.js production build (catches SSR/hydration issues and dead code).
4. Manual browser QA (Task 9) for layout, redirect behavior, and the copy button.
5. Staging deploy verification post-merge.

If a reviewer asks for unit tests, add `@testing-library/react` and `@testing-library/jest-dom` as devDependencies in a follow-up commit and write a single `page.test.tsx` covering the auth-redirect branches with a mocked `useAuth`. That follow-up is intentionally not part of this plan to keep scope tight.
