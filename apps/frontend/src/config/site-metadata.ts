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

/** A page's document title: the page's own, then the site's name. */
export function pageTitle(siteConfig: RuntimeSiteConfig, title: string): string {
  return `${title} | ${siteConfig.branding.appName}`;
}

export function pageMetadata(
  siteConfig: RuntimeSiteConfig,
  title: string,
  description: string,
): Metadata {
  return {
    title: pageTitle(siteConfig, title),
    description,
  };
}
