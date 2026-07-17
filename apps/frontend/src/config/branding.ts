// Centralized site branding (design doc: neutral-upstream split, 6-18 epic P2).
//
// Three-step rule: the FreeInference values below are the compiled-in legacy
// defaults so the shipped build behaves exactly as before; a neutral or
// third-party distribution overrides them via NEXT_PUBLIC_* at build time
// (arrays via *_JSON). Physical removal of the FreeInference defaults waits
// for the overlay-is-truth milestone.
//
// Like env.ts, this is resolved at build time — the bundle is served
// statically, so there is no runtime indirection here. The backend's
// GET /site-config exposes the distribution manifest for runtime consumers;
// wiring the two together is a follow-up once distribution content lands.

import { config } from './env';

export interface TeamMember {
  name: string;
  affiliations: string[];
  badge?: string;
  image?: string;
  website?: string;
}

export interface Sponsor {
  name: string;
  alt: string;
  src: string;
  className: string;
  width: number;
  height: number;
}

function fromJsonEnv<T>(raw: string | undefined, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    // A malformed override must not take the site down; keep the default.
    return fallback;
  }
}

const githubUrl =
  process.env.NEXT_PUBLIC_GITHUB_URL || 'https://github.com/HarvardMadSys/hybridInference';

export const branding = {
  // Product identity (appName itself lives in env.ts and is already
  // NEXT_PUBLIC_APP_NAME-overridable).
  appName: config.appName,
  appDescription:
    process.env.NEXT_PUBLIC_APP_DESCRIPTION ||
    'Free LLM inference for research, built at Harvard SEAS.',
  siteHost: process.env.NEXT_PUBLIC_SITE_HOST || 'freeinference.org',

  // Operating organization.
  orgName: process.env.NEXT_PUBLIC_ORG_NAME || 'Harvard SEAS',
  orgUrl: process.env.NEXT_PUBLIC_ORG_URL || 'https://madsys.seas.harvard.edu',
  orgTagline: process.env.NEXT_PUBLIC_ORG_TAGLINE || 'Built at Harvard SEAS · MadSys Lab',

  // External properties.
  docsUrl: process.env.NEXT_PUBLIC_DOCS_URL || 'https://doc.freeinference.org/',
  statusUrl: process.env.NEXT_PUBLIC_STATUS_URL || 'https://status.staging.freeinference.org/',
  githubUrl,
  commitUrlBase: `${githubUrl}/commit`,
  contactEmail: process.env.NEXT_PUBLIC_CONTACT_EMAIL || 'admin@freeinference.org',

  // Landing-page code example.
  exampleApiBase: process.env.NEXT_PUBLIC_EXAMPLE_API_BASE || 'https://freeinference.org',
  exampleApiKeyEnvVar: process.env.NEXT_PUBLIC_EXAMPLE_API_KEY_ENV_VAR || 'FREEINFERENCE_API_KEY',
  exampleModel: process.env.NEXT_PUBLIC_EXAMPLE_MODEL || 'glm-5.1',

  // Analytics. FreeInference's Statcounter ids remain the legacy default
  // (three-step rule: shipped behavior unchanged); set
  // NEXT_PUBLIC_STATCOUNTER_PROJECT_ID="" to disable entirely. Flipping the
  // default to off is a pre-publication task tracked in the 6-18 epic.
  statcounterProjectId: process.env.NEXT_PUBLIC_STATCOUNTER_PROJECT_ID ?? '13224568',
  statcounterSecurityKey: process.env.NEXT_PUBLIC_STATCOUNTER_SECURITY_KEY ?? '2d8ab84a',

  // Signup fast-track hint (display copy only; the real allowlist is
  // server-side). Empty fastTrackDomain hides the hint.
  fastTrackDomain: process.env.NEXT_PUBLIC_FAST_TRACK_DOMAIN ?? 'harvard.edu',
  fastTrackOrg: process.env.NEXT_PUBLIC_FAST_TRACK_ORG ?? 'Harvard',

  // Namespace for localStorage keys and DOM events. Changing it logs every
  // visitor out of dismissed-state memory; keep it stable per distribution.
  storageKeyPrefix: process.env.NEXT_PUBLIC_STORAGE_KEY_PREFIX || 'freeinference',

  // People and sponsors (empty arrays hide the sections).
  team: fromJsonEnv<TeamMember[]>(process.env.NEXT_PUBLIC_TEAM_JSON, [
    {
      name: 'Juncheng Yang',
      affiliations: ['Assistant Professor at Harvard University'],
      badge: 'Lead',
      image: 'https://junchengyang.com/img/me4.jpg',
    },
    {
      name: 'Murphy Tian',
      affiliations: [
        'Research Intern at Harvard University',
        'Undergraduate at University of Toronto',
      ],
      badge: 'Core developer',
      image: '/team/murphy-tian.jpg',
      website: 'https://realtmxi.github.io/',
    },
    {
      name: 'Haoran Ni',
      affiliations: ['Research Intern at Harvard University', 'Undergraduate at NJU'],
    },
  ]),
  sponsors: fromJsonEnv<Sponsor[]>(process.env.NEXT_PUBLIC_SPONSORS_JSON, [
    {
      name: 'NVIDIA',
      alt: 'NVIDIA logo',
      src: '/sponsors/nvidia.svg',
      className: 'h-10 sm:h-12',
      width: 975,
      height: 180,
    },
    {
      name: 'Harvard SEAS',
      alt: 'Harvard SEAS logo',
      src: '/sponsors/harvard-seas.svg',
      className: 'h-12 sm:h-14',
      width: 307,
      height: 86,
    },
  ]),
};
