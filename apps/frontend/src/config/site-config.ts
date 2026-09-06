import { z } from 'zod';
import { branding as buildTimeBranding, sponsorClassNameSchema, type Branding } from './branding';

function hasProtocol(value: string, protocols: readonly string[]): boolean {
  if (/\s/.test(value) || value.includes('\\')) return false;
  const absolute = /^([A-Za-z][A-Za-z0-9+.-]*):\/\//.exec(value);
  if (!absolute) return false;
  try {
    const parsed = new URL(value);
    return Boolean(parsed.host) && protocols.includes(parsed.protocol);
  } catch {
    return false;
  }
}

const publicLinkSchema = z
  .string()
  .refine((value) => value === '' || hasProtocol(value, ['https:']), 'must be empty or HTTPS');

const publicBaseLinkSchema = z.string().refine((value) => {
  if (value === '') return true;
  if (!hasProtocol(value, ['https:'])) return false;
  return !value.includes('?') && !value.includes('#');
}, 'must be empty or HTTPS without a query or fragment');

const apiBaseSchema = z.string().refine((value) => {
  if (value === '') return true;
  if (!hasProtocol(value, ['http:', 'https:'])) return false;
  return !value.includes('?') && !value.includes('#');
}, 'must be empty or HTTP(S) without a query or fragment');

const assetUrlSchema = z.string().refine((value) => {
  if (value === '') return true;
  if (value.startsWith('/site-assets/') && !value.includes('\\')) return true;
  return hasProtocol(value, ['https:']);
}, 'must be empty, a /site-assets path, or HTTPS');

// A listed nav link must have somewhere to go, so unlike the named links an
// empty url is rejected rather than read as "hidden".
const navLinkSchema = z
  .object({
    label: z.string().trim().min(1),
    url: z.string().refine((value) => hasProtocol(value, ['https:']), 'must be HTTPS'),
  })
  .strict();

const teamMemberSchema = z
  .object({
    name: z.string().trim().min(1),
    affiliations: z.array(z.string().trim().min(1)),
    badge: z.string().trim().min(1).optional(),
    image: assetUrlSchema.optional(),
    website: publicLinkSchema.optional(),
  })
  .strict();

const sponsorSchema = z
  .object({
    name: z.string().trim().min(1),
    alt: z.string().trim().min(1),
    src: assetUrlSchema,
    class_name: sponsorClassNameSchema,
    width: z.number().int().positive(),
    height: z.number().int().positive(),
  })
  .strict();

const runtimeBrandingSchema = z
  .object({
    app_description: z.string(),
    site_host: z.string().trim().min(1),
    organization: z
      .object({
        name: z.string(),
        url: publicLinkSchema,
        tagline: z.string(),
      })
      .strict(),
    links: z
      .object({
        docs_url: publicBaseLinkSchema,
        status_url: publicLinkSchema,
        github_url: publicBaseLinkSchema,
        // Optional in both directions on purpose: the strict object rejects
        // an unknown key, and a required one rejects a document that predates
        // the field. Either would drop a rolling deployment's whole branding
        // back to the neutral build-time values over one added link.
        nav: z.array(navLinkSchema).optional(),
      })
      .strict(),
    example: z
      .object({
        api_base: apiBaseSchema,
        api_key_env_var: z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/),
        model: z.string().trim().min(1),
      })
      .strict(),
    analytics: z
      .object({
        statcounter_project_id: z.string().regex(/^\d*$/),
        statcounter_security_key: z.string().regex(/^[A-Za-z0-9._-]*$/),
      })
      .strict(),
    signup: z
      .object({
        turnstile_site_key: z.string().regex(/^[A-Za-z0-9._-]*$/),
        fast_track_domain: z.string(),
        fast_track_org: z.string(),
      })
      .strict(),
    storage_key_prefix: z.string().regex(/^[A-Za-z0-9._:-]+$/),
    data_policy_notice: z.string(),
    assets: z
      .object({
        logo_url: assetUrlSchema,
        favicon_url: assetUrlSchema,
      })
      .strict(),
    team: z.array(teamMemberSchema),
    sponsors: z.array(sponsorSchema),
  })
  .strict();

const distributionSchema = z
  .object({
    id: z.string().trim(),
    display_name: z.string().trim(),
    release: z.string(),
  })
  .strict();

const siteSchema = z
  .object({
    public_base_url: z.string(),
    support_email: z.string(),
  })
  .strict();

const featuresSchema = z
  .object({
    routers: z.array(z.string()),
    public_signup: z.boolean().nullable(),
    rag: z.boolean().nullable(),
  })
  .strict();

const versionedSiteConfigDocumentSchema = z
  .object({
    schema_version: z.literal(1),
    distribution: distributionSchema,
    site: siteSchema,
    features: featuresSchema,
    // Parse branding independently. A deployment can roll the v1 endpoint out
    // before its branding document without replacing the compatibility values
    // already baked into the transition image.
    branding: z.unknown().nullable(),
  })
  .strict();

// The endpoint immediately preceding schema v1 exposed this exact subset.
// Accept it during rolling upgrades and paired rollbacks so a neutral console
// keeps the deployment identity and feature gates while the backend catches up.
const legacySiteConfigDocumentSchema = z
  .object({
    distribution: distributionSchema,
    site: siteSchema,
    features: featuresSchema,
  })
  .strict();

const siteConfigDocumentSchema = z.union([
  versionedSiteConfigDocumentSchema,
  legacySiteConfigDocumentSchema,
]);

export interface RuntimeSiteConfig {
  branding: Branding;
  distribution: {
    id: string;
    release: string;
  };
  features: {
    publicSignup: boolean;
    rag: boolean;
    agents: boolean;
  };
}

export const buildTimeSiteConfig: RuntimeSiteConfig = {
  branding: buildTimeBranding,
  distribution: { id: 'legacy', release: '' },
  features: { publicSignup: true, rag: true, agents: false },
};

function resolveBranding(input: unknown, displayName: string, supportEmail: string): Branding {
  if (input === null) return buildTimeBranding;

  const parsed = runtimeBrandingSchema.safeParse(input);
  if (!parsed.success) return buildTimeBranding;

  const document = parsed.data;
  const githubUrl = document.links.github_url.replace(/\/+$/, '');
  return {
    appName: displayName || buildTimeBranding.appName,
    appDescription: document.app_description,
    siteHost: document.site_host,
    orgName: document.organization.name,
    orgUrl: document.organization.url,
    orgTagline: document.organization.tagline,
    docsUrl: document.links.docs_url,
    statusUrl: document.links.status_url,
    githubUrl,
    navLinks: document.links.nav ?? [],
    commitUrlBase: githubUrl ? `${githubUrl}/commit` : '',
    contactEmail: supportEmail.trim(),
    exampleApiBase: document.example.api_base.replace(/\/+$/, ''),
    exampleApiKeyEnvVar: document.example.api_key_env_var,
    exampleModel: document.example.model,
    statcounterProjectId: document.analytics.statcounter_project_id,
    statcounterSecurityKey: document.analytics.statcounter_security_key,
    turnstileSiteKey: document.signup.turnstile_site_key,
    fastTrackDomain: document.signup.fast_track_domain,
    fastTrackOrg: document.signup.fast_track_org,
    dataPolicyNotice: document.data_policy_notice,
    storageKeyPrefix: document.storage_key_prefix,
    logoUrl: document.assets.logo_url,
    faviconUrl: document.assets.favicon_url,
    team: document.team,
    sponsors: document.sponsors.map((sponsor) => ({
      name: sponsor.name,
      alt: sponsor.alt,
      src: sponsor.src,
      className: sponsor.class_name,
      width: sponsor.width,
      height: sponsor.height,
    })),
  };
}

function resolveLegacyBranding(
  displayName: string,
  publicBaseUrl: string,
  supportEmail: string,
): Branding {
  const normalizedBaseUrl = publicBaseUrl.replace(/\/+$/, '');
  const safeBaseUrl = apiBaseSchema.safeParse(normalizedBaseUrl).success ? normalizedBaseUrl : '';
  let siteHost = '';
  if (safeBaseUrl) {
    try {
      siteHost = new URL(safeBaseUrl).host;
    } catch {
      // The schema already checks this, but keep the fallback local if URL
      // parsing behavior differs between runtimes.
    }
  }

  return {
    ...buildTimeBranding,
    ...(displayName ? { appName: displayName } : {}),
    ...(safeBaseUrl ? { exampleApiBase: safeBaseUrl } : {}),
    ...(siteHost ? { siteHost } : {}),
    ...(supportEmail.trim() ? { contactEmail: supportEmail.trim() } : {}),
  };
}

export function resolveRuntimeSiteConfig(input: unknown): RuntimeSiteConfig {
  const parsed = siteConfigDocumentSchema.safeParse(input);
  if (!parsed.success) return buildTimeSiteConfig;

  const document = parsed.data;
  let branding: Branding;
  if ('schema_version' in document) {
    branding = resolveBranding(
      document.branding,
      document.distribution.display_name,
      document.site.support_email,
    );
  } else {
    branding = resolveLegacyBranding(
      document.distribution.display_name,
      document.site.public_base_url,
      document.site.support_email,
    );
  }

  return {
    branding,
    distribution: {
      id: document.distribution.id,
      release: document.distribution.release,
    },
    features: {
      publicSignup: document.features.public_signup ?? buildTimeSiteConfig.features.publicSignup,
      rag: document.features.rag ?? buildTimeSiteConfig.features.rag,
      // Never trust or infer this from public JSON. Only the server-side
      // loader may enable it after seeing both private proxy destinations.
      agents: false,
    },
  };
}

export function withAgentsFeature(
  siteConfig: RuntimeSiteConfig,
  agents: boolean,
): RuntimeSiteConfig {
  return {
    ...siteConfig,
    features: { ...siteConfig.features, agents },
  };
}
