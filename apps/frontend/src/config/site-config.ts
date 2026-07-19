import { z } from 'zod';
import { branding as buildTimeBranding, type Branding } from './branding';

const siteConfigDocumentSchema = z.object({
  distribution: z.object({
    id: z.string(),
    display_name: z.string(),
    release: z.string(),
  }),
  site: z.object({
    public_base_url: z.string(),
    support_email: z.string(),
    description: z.string(),
  }),
  features: z.object({
    routers: z.array(z.string()),
    public_signup: z.boolean().nullable(),
    rag: z.boolean().nullable(),
  }),
});

export interface RuntimeSiteConfig {
  branding: Branding;
  distribution: {
    id: string;
    release: string;
  };
  features: {
    publicSignup: boolean;
    rag: boolean;
  };
}

export const buildTimeSiteConfig: RuntimeSiteConfig = {
  branding: buildTimeBranding,
  distribution: { id: 'legacy', release: '' },
  features: { publicSignup: true, rag: true },
};

function hostFromBaseUrl(value: string): string | undefined {
  if (!value) return undefined;
  try {
    return new URL(value).host || undefined;
  } catch {
    return undefined;
  }
}

export function resolveRuntimeSiteConfig(input: unknown): RuntimeSiteConfig {
  const parsed = siteConfigDocumentSchema.safeParse(input);
  if (!parsed.success) return buildTimeSiteConfig;

  const document = parsed.data;
  const publicBaseUrl = document.site.public_base_url.replace(/\/+$/, '');
  const siteHost = hostFromBaseUrl(publicBaseUrl);
  const displayName = document.distribution.display_name.trim();
  const supportEmail = document.site.support_email.trim();
  const description = document.site.description.trim();

  return {
    branding: {
      ...buildTimeBranding,
      ...(displayName ? { appName: displayName } : {}),
      ...(publicBaseUrl ? { exampleApiBase: publicBaseUrl } : {}),
      ...(siteHost ? { siteHost } : {}),
      ...(supportEmail ? { contactEmail: supportEmail } : {}),
      ...(description ? { appDescription: description } : {}),
    },
    distribution: {
      id: document.distribution.id,
      release: document.distribution.release,
    },
    features: {
      publicSignup: document.features.public_signup ?? buildTimeSiteConfig.features.publicSignup,
      rag: document.features.rag ?? buildTimeSiteConfig.features.rag,
    },
  };
}
