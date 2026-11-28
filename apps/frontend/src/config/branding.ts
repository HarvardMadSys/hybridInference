// Centralized site branding (design doc: neutral-upstream split, 6-18 epic P2).
//
// Three-step rule, step 2: defaults that would misdirect a third-party
// deployment (analytics ids, the API host in the copy-paste example, the
// affiliated-domain hint) are neutral or off. A distribution supplies its
// identity through the versioned runtime document instead.
//
// These values are the neutral console's branding, used while the gateway
// serves no SITE_BRANDING document. The root server layout loads
// GET /site-config at request time and gives SiteConfigProvider that value on
// its first render, so one frontend image follows the active distribution
// without a client-side brand flash. No build argument changes them.

import { z } from 'zod';

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
  // Published API base for copy-paste examples; '' when the deployment
  // publishes none and the console derives one from its own API origin.
  exampleApiBase: string;
  exampleApiKeyEnvVar: string;
  exampleModel: string;
  // True only when a runtime branding document sets example.api_base to
  // '' on purpose: the documented "hidden example", which suppresses the
  // quickstart instead of deriving a base.
  exampleHidden: boolean;
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

const APP_NAME = 'HybridInference';

// The upstream project's own repository: it identifies the software, not a
// deployment of it.
const githubUrl = 'https://github.com/HarvardMadSys/hybridInference';

export const branding: Branding = {
  appName: APP_NAME,
  appDescription: 'A gateway that routes LLM requests across local and remote inference providers.',
  // Reads as a name in prose ("Why X", "How did you find X?"), so it is the
  // product name rather than an empty string.
  siteHost: APP_NAME,

  // Operating organization and external properties: empty means "this
  // deployment has none", and every consumer hides the corresponding link
  // or sentence rather than rendering an empty href.
  orgName: '',
  orgUrl: '',
  orgTagline: '',
  docsUrl: '',
  statusUrl: '',
  githubUrl,
  // Extra header links are distribution identity by definition, so the
  // neutral console ships none and only the runtime document adds any.
  navLinks: [],
  commitUrlBase: `${githubUrl}/commit`,
  contactEmail: '',

  // Homepage code example. The neutral console publishes no example base of
  // its own: the developer home derives one from NEXT_PUBLIC_API_BASE (else
  // the page origin), so a console built against a real API never advertises
  // a localhost default, and never someone else's hosted service. The model id
  // matches config/examples/models.openrouter.json.
  exampleApiBase: '',
  exampleApiKeyEnvVar: 'HYBRIDINFERENCE_API_KEY',
  exampleModel: 'llama-3.3-70b',
  exampleHidden: false,

  // Analytics: off unless an operator opts in with their own Statcounter ids
  // (layout.tsx skips the scripts entirely when the project id is empty).
  // A shipped default would report every third-party deployment's traffic
  // into an upstream operator's account; those ids come from the deployment.
  statcounterProjectId: '',
  statcounterSecurityKey: '',

  // CAPTCHA is public configuration (the secret key remains backend-only);
  // a deployment sets its site key in the runtime branding document.
  turnstileSiteKey: '',

  // Signup fast-track hint (display copy only; the real allowlist is
  // server-side). Empty fastTrackDomain hides the hint, which is the right
  // default for a deployment that has no affiliated domain.
  fastTrackDomain: '',
  fastTrackOrg: '',

  // Data-handling notice shown on the landing page. Empty by default and
  // hidden when empty: a deployment must state its own policy, and no
  // upstream default can be correct for someone else's users. A distribution
  // supplies its own text through /site-config.
  dataPolicyNotice: '',

  // Namespace for persisted browser preferences. Changing it resets existing
  // dismissal state, so keep it stable per distribution.
  storageKeyPrefix: 'hybridinference',

  // Runtime distributions normally serve these from /site-assets. Empty
  // fallbacks preserve the neutral console's existing text-only header and
  // browser-default icon.
  logoUrl: '',
  faviconUrl: '',

  // People and sponsors. Empty upstream — a neutral console has neither, and
  // arrays hidden behind a fallback are the kind of content that silently
  // ships one distribution's identity to everyone else. A distribution lists
  // its own through /site-config. Empty arrays hide the sections.
  team: [],
  sponsors: [],
};
