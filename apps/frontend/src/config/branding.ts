// Centralized site branding (design doc: neutral-upstream split, 6-18 epic P2).
//
// Three-step rule, step 2: defaults that would misdirect a third-party
// deployment (analytics ids, the API host in the copy-paste example, the
// affiliated-domain hint) are now neutral or off. FreeInference keeps its
// identity because deploy/docker/docker-compose.yml passes an explicit value
// for every one of these at build time, so its rendered output is unchanged.
//
// Still carrying FreeInference defaults, deliberately: team and sponsors,
// whose compose default is empty — meaning the arrays below are what
// production actually renders today. They move once their data is supplied
// through NEXT_PUBLIC_TEAM_JSON / NEXT_PUBLIC_SPONSORS_JSON.
//
// These values are the build-time fallback. SiteConfigProvider overlays the
// safe identity and feature fields from GET /site-config at browser runtime,
// so one frontend image can follow the active distribution manifest.

import { z } from 'zod';
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
    className: z.string(),
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

export const branding = {
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
  // into the FreeInference account; those ids now come from the deployment
  // (docker-compose passes them for FreeInference builds).
  statcounterProjectId: process.env.NEXT_PUBLIC_STATCOUNTER_PROJECT_ID ?? '',
  statcounterSecurityKey: process.env.NEXT_PUBLIC_STATCOUNTER_SECURITY_KEY ?? '',

  // Signup fast-track hint (display copy only; the real allowlist is
  // server-side). Empty fastTrackDomain hides the hint, which is the right
  // default for a deployment that has no affiliated domain.
  fastTrackDomain: process.env.NEXT_PUBLIC_FAST_TRACK_DOMAIN ?? '',
  fastTrackOrg: process.env.NEXT_PUBLIC_FAST_TRACK_ORG ?? '',

  // Data-handling notice shown on the landing page. Empty by default and
  // hidden when empty: a deployment must state its own policy, and no
  // upstream default can be correct for someone else's users. FreeInference
  // supplies its text through the deployment (docker-compose).
  dataPolicyNotice: process.env.NEXT_PUBLIC_DATA_POLICY_NOTICE || '',

  // Namespace for localStorage keys and DOM events. Changing it logs every
  // visitor out of dismissed-state memory; keep it stable per distribution.
  storageKeyPrefix: process.env.NEXT_PUBLIC_STORAGE_KEY_PREFIX || 'hybridinference',

  // People and sponsors. Empty upstream — a neutral console has neither, and
  // arrays hidden behind a fallback are the kind of content that silently
  // ships one distribution's identity to everyone else. FreeInference passes
  // its own through NEXT_PUBLIC_TEAM_JSON / NEXT_PUBLIC_SPONSORS_JSON
  // (deploy/docker/docker-compose.yml). Empty arrays hide the sections.
  team: fromJsonEnv<TeamMember[]>(process.env.NEXT_PUBLIC_TEAM_JSON, [], teamSchema),
  sponsors: fromJsonEnv<Sponsor[]>(process.env.NEXT_PUBLIC_SPONSORS_JSON, [], sponsorsSchema),
};

export type Branding = typeof branding;
