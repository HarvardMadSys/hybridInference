// Centralized site branding (design doc: neutral-upstream split, 6-18 epic P2).
//
// Three-step rule, step 2: defaults that would misdirect a third-party
// deployment (analytics ids, the API host in the copy-paste example, the
// affiliated-domain hint) are neutral or off. A distribution supplies its
// identity through the versioned runtime document instead.
//
// These values are the transition build fallback. The root server layout
// loads GET /site-config at request time and gives SiteConfigProvider that
// value on its first render, so one frontend image can follow the active
// distribution manifest without a client-side brand flash.

import { z } from 'zod';
import { config } from './env';

export interface TeamMember {
  name: string;
  affiliations: string[];
  badge?: string;
  image?: string;
  website?: string;
}

export interface NavLink {
  label: string;
  url: string;
}

export interface Sponsor {
  name: string;
  alt: string;
  src: string;
  className: string;
  width: number;
  height: number;
}

// Runtime Tailwind classes must come from the finite set safelisted in
// tailwind.config.ts. Accepting arbitrary deployment strings would render
// classes the neutral build never compiled and widen the public contract.
export const sponsorClassNameSchema = z
  .string()
  .regex(/^(?:h-(?:8|10|12|14|16))(?: sm:h-(?:8|10|12|14|16))?$/);

export interface Branding {
  appName: string;
  appDescription: string;
  siteHost: string;
  orgName: string;
  orgUrl: string;
  orgTagline: string;
  docsUrl: string;
  statusUrl: string;
  githubUrl: string;
  navLinks: NavLink[];
  commitUrlBase: string;
  contactEmail: string;
  exampleApiBase: string;
  exampleApiKeyEnvVar: string;
  exampleModel: string;
  statcounterProjectId: string;
  statcounterSecurityKey: string;
  turnstileSiteKey: string;
  fastTrackDomain: string;
  fastTrackOrg: string;
  dataPolicyNotice: string;
  storageKeyPrefix: string;
  logoUrl: string;
  faviconUrl: string;
  team: TeamMember[];
  sponsors: Sponsor[];
}

const teamSchema: z.ZodType<TeamMember[]> = z.array(
  z.object({
    name: z.string().min(1),
    affiliations: z.array(z.string().min(1)),
    badge: z.string().min(1).optional(),
    image: z.string().min(1).optional(),
    website: z.string().min(1).optional(),
  }),
);

const sponsorsSchema: z.ZodType<Sponsor[]> = z.array(
  z.object({
    name: z.string().min(1),
    alt: z.string().min(1),
    src: z.string().min(1),
    className: sponsorClassNameSchema,
    width: z.number().positive(),
    height: z.number().positive(),
  }),
);

function fromJsonEnv<T>(raw: string | undefined, fallback: T, schema: z.ZodType<T>): T {
  if (!raw) return fallback;
  try {
    const parsed = schema.safeParse(JSON.parse(raw));
    return parsed.success ? parsed.data : fallback;
  } catch (error) {
    // A malformed or wrong-shaped override must not take the site down.
    if (error instanceof SyntaxError) return fallback;
    throw error;
  }
}

const githubUrl =
  process.env.NEXT_PUBLIC_GITHUB_URL || 'https://github.com/HarvardMadSys/hybridInference';

export const branding: Branding = {
  // Product identity (appName itself lives in env.ts and is already
  // NEXT_PUBLIC_APP_NAME-overridable).
  appName: config.appName,
  appDescription:
    process.env.NEXT_PUBLIC_APP_DESCRIPTION ||
    'A gateway that routes LLM requests across local and remote inference providers.',
  // Reads as a name in prose ("Why X", "How did you find X?"), so it falls
  // back to the product name rather than to an empty string.
  siteHost: process.env.NEXT_PUBLIC_SITE_HOST || config.appName,

  // Operating organization and external properties: empty means "this
  // deployment has none", and every consumer hides the corresponding link
  // or sentence rather than rendering an empty href.
  orgName: process.env.NEXT_PUBLIC_ORG_NAME || '',
  orgUrl: process.env.NEXT_PUBLIC_ORG_URL || '',
  orgTagline: process.env.NEXT_PUBLIC_ORG_TAGLINE || '',
  docsUrl: process.env.NEXT_PUBLIC_DOCS_URL || '',
  statusUrl: process.env.NEXT_PUBLIC_STATUS_URL || '',
  githubUrl,
  // Extra header links are distribution identity by definition, so the
  // neutral console ships none and only the runtime document adds any.
  navLinks: [],
  commitUrlBase: `${githubUrl}/commit`,
  contactEmail: process.env.NEXT_PUBLIC_CONTACT_EMAIL || '',

  // Landing-page code example. The default must be copy-pasteable against the
  // reader's own deployment, never against someone else's hosted service; the
  // model id matches config/examples/models.openrouter.yaml.
  exampleApiBase: process.env.NEXT_PUBLIC_EXAMPLE_API_BASE || 'http://localhost:8080',
  exampleApiKeyEnvVar: process.env.NEXT_PUBLIC_EXAMPLE_API_KEY_ENV_VAR || 'HYBRIDINFERENCE_API_KEY',
  exampleModel: process.env.NEXT_PUBLIC_EXAMPLE_MODEL || 'llama-3.3-70b',

  // Analytics: off unless an operator opts in with their own Statcounter ids
  // (layout.tsx skips the scripts entirely when the project id is empty).
  // A shipped default would report every third-party deployment's traffic
  // into an upstream operator's account; those ids come from the deployment.
  statcounterProjectId: process.env.NEXT_PUBLIC_STATCOUNTER_PROJECT_ID ?? '',
  statcounterSecurityKey: process.env.NEXT_PUBLIC_STATCOUNTER_SECURITY_KEY ?? '',

  // CAPTCHA is public configuration (the secret key remains backend-only).
  // Keep the build-time value as a compatibility fallback until every
  // deployment serves the versioned runtime branding document.
  turnstileSiteKey: process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY ?? '',

  // Signup fast-track hint (display copy only; the real allowlist is
  // server-side). Empty fastTrackDomain hides the hint, which is the right
  // default for a deployment that has no affiliated domain.
  fastTrackDomain: process.env.NEXT_PUBLIC_FAST_TRACK_DOMAIN ?? '',
  fastTrackOrg: process.env.NEXT_PUBLIC_FAST_TRACK_ORG ?? '',

  // Data-handling notice shown on the landing page. Empty by default and
  // hidden when empty: a deployment must state its own policy, and no
  // upstream default can be correct for someone else's users. A distribution
  // supplies its own text through /site-config.
  dataPolicyNotice: process.env.NEXT_PUBLIC_DATA_POLICY_NOTICE || '',

  // Namespace for persisted browser preferences. Changing it resets existing
  // dismissal state, so keep it stable per distribution.
  storageKeyPrefix: process.env.NEXT_PUBLIC_STORAGE_KEY_PREFIX || 'hybridinference',

  // Runtime distributions normally serve these from /site-assets. Empty
  // fallbacks preserve the neutral console's existing text-only header and
  // browser-default icon.
  logoUrl: '',
  faviconUrl: '',

  // People and sponsors. Empty upstream — a neutral console has neither, and
  // arrays hidden behind a fallback are the kind of content that silently
  // ships one distribution's identity to everyone else. Legacy images may
  // still supply NEXT_PUBLIC_TEAM_JSON / NEXT_PUBLIC_SPONSORS_JSON; runtime
  // distributions use /site-config. Empty arrays hide the sections.
  team: fromJsonEnv<TeamMember[]>(process.env.NEXT_PUBLIC_TEAM_JSON, [], teamSchema),
  sponsors: fromJsonEnv<Sponsor[]>(process.env.NEXT_PUBLIC_SPONSORS_JSON, [], sponsorsSchema),
};
