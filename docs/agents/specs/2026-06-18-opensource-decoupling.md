# Epic: Decouple user-management & Harvard-specifics for open-source deployment

**Type:** Epic / tracking issue
**Status:** Proposed
**Goal:** Make HybridInference deployable by anyone — not just `freeinference.org` — without editing source.

> Chinese version: [2026-06-18-opensource-decoupling.zh.md](2026-06-18-opensource-decoupling.zh.md)

> **Publication decision superseded on 2026-08-26.** The existing
> `HarvardMadSys/hybridInference` repository will become public. There is no
> filtered export, so both its current tree and every retained Git ref are in
> scope for the pre-publication audit. See the
> [direct-publication readiness plan](../plans/2026-08-26-direct-publication-readiness.md).
> Later statements in this historical epic that say the repository history
> will stay private are no longer operative.

> **Revision note (2026-07-16, aligned with the main design doc):** P0–P2 of this
> epic serve as the file-level implementation checklist for Phase 1 of the
> [neutral-upstream / distribution split design](2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md);
> P3–P4 block nothing there and are scheduled after its Phase 3, on demand.
> Three prescriptions revised (the body below has been rewritten accordingly;
> this note records the deltas): (1) security-critical section — the
> account/database IDs in `wrangler.toml` are identifiers, not credentials (no
> committed secret exists to rotate); rotate `CLOUDFLARE_API_TOKEN` as cheap
> insurance; the concrete publication mechanism is tracked in the main doc's
> "Publication & Visibility" section. Making Statcounter env-driven remains a hard
> pre-publication item. (2) P1/P2 defaults and branding content — now three-step: legacy
> FreeInference defaults/assets stay, a neutral profile supplies generic
> values or hides them, and removal happens only after the overlay becomes
> production truth. (3) The "unlock
> the RouteWise dependency" item in P5 is a standalone decision: both options
> amount to publishing RouteWise code, and today `strategies/__init__.py`
> hard-imports it at startup while `routing/routewise/` imports
> `routewise.core` in several modules, so the real effort is M/L — it has been
> promoted to the main doc's Phase 3 entry gate.

---

## Motivation

HybridInference solves a problem with broad value: a **single OpenAI-compatible
endpoint** that routes requests across local inference servers (vLLM / SGLang /
Ollama) and many remote providers, with health checks, circuit breaking, and
cost-aware routing (RouteWise). That capability is useful far beyond Harvard —
any lab, company, or individual wanting to stand up their own unified
low-cost inference gateway needs it.

But **today it can only run as `freeinference.org`**: identity, branding,
user-management policy, and even infrastructure IDs are baked into the source.
Anyone who wants the same capability has to fork and gut it — wasting the reach
this system could have, and starving routing research like RouteWise of
real-world validation and outside contributors.

**Core thesis: what blocks open-sourcing is coupling, not capability.** The core
(routing, adapters, streaming, storage abstraction) is already generic; the two
things that actually block us are (1) the user-management system bakes in **one
organization's policy** (approval workflow, quotas, domain allowlist) as the only
option, and (2) Harvard's identity and infrastructure are hardcoded. A fresh
deployer wants to choose their own auth posture (no auth / their own SSO), their
own brand, and their own policy — and must **never** inherit Harvard's infra or
identity.

So the goal of this epic is: **decouple so the default open-source build is
neutral and runnable by anyone out of the box, and `freeinference.org`'s
specifics become one configuration layered on top.**

---

## Summary

The gateway's architecture is already clean enough to open-source: the routing
engine, adapter framework, HTTP/SSE plumbing, and the two-tier storage
abstraction (`OperationalStore` / `LogStore`) are generic and ship as-is. The
Harvard coupling is **not** structural — it lives in four places:

1. ~30 hardcoded `freeinference.org` strings used as code defaults.
2. **Live Harvard infrastructure leaked into source/history** (Cloudflare
   account + D1 IDs, Statcounter account).
3. Frontend branding (team bios, sponsor logos, `@harvard.edu` copy).
4. The implicit assumption that auth / quota / approval are *always on*.

So this is a **config-extraction + feature-gating** effort, not a core rewrite.

**Recommended approach:** a feature-flag-driven monolith with thin interfaces at
the existing `Depends()` seams — **not** plugin packages, **not** a fork. The
code already injects auth/quota/concurrency via FastAPI `Depends()` and has a
`runtime_settings` registry plus `USER_AUTH_ENABLED`; we extend that pattern.

Related: #642 (decouple router from service).

---

## ⚠️ Security-critical — must land before the repo goes public

These leak real Harvard infrastructure and are the hard blockers:

- [ ] **Cloudflare account + D1 database IDs in `wrangler.toml`** —
  `services/status-monitor-worker/wrangler.toml:7,37,38`. These are
  identifiers, not credentials (the file itself documents `account_id` as
  non-secret; the actual secret, `CLOUDFLARE_API_TOKEN`, was never committed).
  Parameterize them out of HEAD and rotate `CLOUDFLARE_API_TOKEN` as cheap
  insurance. Audit every retained Git ref before publication; if private data
  is present, clean it during the coordinated history migration.
- [ ] **Statcounter analytics block** — `apps/frontend/src/app/layout.tsx:27,31,57`
  (project `13224568`, security key `2d8ab84a`). Every deployer's traffic would
  flow into Harvard's analytics account. Gate behind
  `NEXT_PUBLIC_STATCOUNTER_PROJECT_ID`, default-off.
- [ ] Re-audit `.gitleaks.toml` so rotated/real values aren't suppressed by the
  existing allowlist rule.

---

## Scope

### In scope
- Backend config extraction (remove hardcoded `freeinference.org` defaults).
- Feature-gating auth / quota / concurrency / admin / email behind flags with
  permissive no-op defaults.
- Frontend branding/theming made config-driven.
- Documentation, license, packaging for self-hosting.

### Out of scope (no behavior change for the live deployment)
- Routing engine, adapters, SSE/streaming hot path (ships as-is).
- Storage schema / migrations (already generic and deployment-agnostic).
- The signup domain allowlist mechanism itself — it is **already** data-driven
  (empty `signup_allowed_domains` table = allow-all + auto-approve); only the
  frontend's hardcoded `@harvard.edu` copy and docs need changing.

---

## Layer map

| Layer | Belongs here | Action |
|---|---|---|
| **(a) Reusable core** | routing engine, adapters, HTTP/SSE, storage abstraction, JWT/bcrypt, `runtime_settings` | Ship as-is (one fix: `adapters/openrouter.py:11-12`) |
| **(b) Pluggable / optional** | API-key verify + quota (`servers/auth.py:88-299`), per-user concurrency (`concurrency.py:203-325`), model gating (`completions.py:495-552`, `model_access.py:25-30`), admin surface, email | Behind flags, default no-op/permissive |
| **(c) Deployment config** | URLs, emails, CORS, DB name, role quota/concurrency defaults, `NEXT_PUBLIC_*`, `models.yaml` | env / config files; FreeInference defaults stay until the overlay is production truth (see P1) |
| **(d) Harvard-only** | team page, sponsor logos, Harvard SEAS metadata, `@harvard.edu` copy, Statcounter, Cloudflare IDs, LICENSE copyright, RouteWise pin | delete or make config-driven |

---

## Phased plan (each phase independently shippable)

### P0 — Infra & secret extraction (effort: S) — **do first**
- [ ] Parameterize `wrangler.toml` `account_id` / `database_id` /
      `database_name` / `GATEWAY_BASE_URL` (`:7,17,37,38`) via Wrangler env vars.
- [ ] Rotate `CLOUDFLARE_API_TOKEN` as insurance and clean retained history as
      required by the readiness audit.
- [ ] Make the status-monitor worker an **optional** add-on, not a prerequisite.

### P1 — Backend config-extraction sweep (effort: S/M)
Replace every `freeinference.org` / `admin@freeinference.org` literal with
env-backed `Settings` fields. Do **not** change the shipped defaults yet:
legacy FreeInference defaults stay in place, a **neutral profile** supplies
the generic values (`example.com`, `localhost`), and the legacy defaults are
removed only after the distribution overlay has become production truth —
this keeps the main design doc's "default FreeInference behavior unchanged"
invariant:
- [ ] `QUOTA_CONTACT_EMAIL` — 3 sites: `servers/auth.py:28`,
      `servers/routers/user_routes.py`, `schemas_auth.py` (QuotaInfo/QuotaExceeded).
- [ ] `base_url` / `frontend_url` — `config/settings.py:82,85` (+ `.env.example`).
- [ ] `smtp_from_email` / `smtp_from_name` — `config/settings.py:78,79`.
- [ ] `db_name` default `freeinference_db` — `config/settings.py:20`.
- [ ] CORS origins — `config/settings.py:110-116` → parse `CORS_ALLOWED_ORIGINS`.
- [ ] OpenRouter `HTTP-Referer` / `X-Title` → env vars — `adapters/openrouter.py:11-12`.
- [ ] Per-role quota (`runtime_settings.py:107-138`) and concurrency
      (`:83-106`) defaults → `config/quotas.yaml` / env overrides.

### P2 — Frontend branding config (effort: M)
Same three-step rule as P1: FreeInference content stays as the compiled-in
legacy default, the neutral profile omits/hides it, and deletion happens only
after the distribution overlay is production truth.
- [ ] Add `apps/frontend/src/config/branding.ts` (or `branding.json`):
      `{ appName, orgName, labUrl, docsUrl, statusUrl, githubRepo, supportEmail,
      sponsors[], teamMembers[], showTeamPage }`, all `NEXT_PUBLIC_*`-overridable.
- [ ] Rewire Hero, Header, SiteFooter, Sponsors, Team, Terms, BuildInfo,
      CodeExample, Features, `layout.tsx` metadata, `config/env.ts` apiBase.
- [ ] Hide team/sponsor sections when their config is empty.
- [ ] Gate Statcounter on `NEXT_PUBLIC_STATCOUNTER_PROJECT_ID` (default-off).
- [ ] Move Harvard assets (`public/team/murphy-tian.jpg`,
      `public/sponsors/harvard-seas.svg`) and the `junchengyang.com` entry in
      `next.config.js` behind the branding config as FreeInference defaults;
      physical removal waits for the overlay-is-truth milestone.
- [ ] Replace `@harvard.edu` fast-track copy (`signup/page.tsx:148-150,236`,
      `lib/schemas/auth.ts`) with config (`NEXT_PUBLIC_FAST_TRACK_DOMAIN`,
      `NEXT_PUBLIC_SIGNUP_REQUIRES_REVIEW`); hide hint when unset.

### P3 — Auth / quota / concurrency feature-gating (effort: M/L) — largest
- [ ] Introduce `AuthProvider` protocol with in-tree impls: `NoAuthProvider`
      (allow-all, selected when `USER_AUTH_ENABLED=false`), `ApiKeyAuthProvider`
      (current logic from `servers/auth.py`), `ExternalJwtAuthProvider`
      (bring-your-own-SSO, reuses `utils/jwt.py`).
- [ ] Extract the quota + email-verification checks currently inlined in
      `verify_api_key` (`auth.py:205-225`, `227-280`) into a separate
      `QuotaEnforcer` (`NoQuota` default vs `DailyCostQuota`) and a verification
      gate so each disables independently. **Keep the quota math byte-for-byte
      in `DailyCostQuota`; add `NoQuota` as a new class — do not refactor the
      math while extracting.**
- [ ] `ConcurrencyLimiter` (`NoLimit` vs current `concurrency.py:203-325`).
- [ ] `ModelGate` (`AllowAll` vs role visibility + denylist).
- [ ] New flags: `QUOTA_ENABLED`, `CONCURRENCY_ENABLED`, `MODEL_GATING_ENABLED`,
      `ADMIN_ENABLED` (admin routes return 404 when off).
- [ ] **Verify the `NoAuth + NoQuota` path against a streaming completion** —
      no-op enforcers must be synchronous, allocation-light, and must not buffer
      the SSE response (middleware order matters).

### P4 — Pluggable email + external IdP (effort: M)
- [ ] Make `SendEmailBackend` swappable: SMTP / console / no-op
      (`is_email_enabled()` already half-does this).
- [ ] Ship `ExternalJwtAuthProvider` (issuer / JWKS URL from env).
- [ ] Make Turnstile optional (`ENABLE_TURNSTILE`, default off).

### P5 — Docs, license, packaging (effort: S/M)
- [ ] Rebrand README / docs / AGENTS.md; parameterize GitHub org references.
- [ ] Update LICENSE copyright line (`LICENSE:3`).
- [ ] Resolve the RouteWise dependency (`pyproject.toml:31`) per the main
      doc's Phase 3 entry gate: publish RouteWise (wheel or public repo) or
      vendor it into the upstream. An install-optional extra is **not** an
      option — RouteWise is a first-party upstream algorithm and
      `strategies/__init__.py` hard-imports it at startup, so the core must
      ship with it.
- [ ] Ship `.env.example` with generic values, `config/models.example.yaml`
      (placeholder endpoints, no real aliases), and `examples/` Docker
      Compose / systemd with placeholders.

---

## Risks & gotchas
- **Streaming / middleware ordering (high):** P3 inserts auth/quota decisions
  before routing in the hot path; no-op enforcers must not buffer the SSE
  response. Test the no-auth streaming path explicitly.
- **Committed identifiers (low, revised):** see security section — rotate the
  API token as insurance, parameterize HEAD, and include retained history in
  the direct-publication audit.
- **License (low/med):** MIT is fine, but the copyright names Harvard SEAS;
  confirm RouteWise is itself redistributable or make it optional.
- **D1 vs Postgres (med):** main stores are Postgres with a clean abstraction;
  the status-monitor D1 is fully decoupled and should be an optional add-on.
  Don't let "we support D1" imply the gateway needs Cloudflare.
- **Admin bootstrap:** `ADMIN_EMAILS` auto-promotes matching users to admin at
  login/boot — keep this as the documented first-admin provisioning path; it
  solves the empty-DB chicken-and-egg problem.

---

## Acceptance criteria
- [ ] A fresh clone runs end-to-end under the **neutral profile** with no
      `freeinference.org` / Harvard string in that profile's build or config
      (legacy FreeInference defaults may remain until the overlay is
      production truth).
- [ ] `USER_AUTH_ENABLED=false` serves chat completions (incl. streaming) with
      no DB-backed user state.
- [ ] No live Harvard credentials or private infrastructure data in the
      current tree or any retained Git ref.
- [ ] A deployer can set their own brand, support email, and (optionally) bring
      their own SSO without editing source.
