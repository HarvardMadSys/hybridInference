import type { Metadata } from 'next';
import type { RuntimeSiteConfig } from './site-config';

export function rootMetadata(siteConfig: RuntimeSiteConfig): Metadata {
  const { branding } = siteConfig;
  return {
    title: branding.appName,
    description: branding.appDescription,
    ...(branding.faviconUrl ? { icons: { icon: branding.faviconUrl } } : {}),
  };
}

export function pageMetadata(
  siteConfig: RuntimeSiteConfig,
  pageTitle: string,
  description: string,
): Metadata {
  return {
    title: `${pageTitle} | ${siteConfig.branding.appName}`,
    description,
  };
}
