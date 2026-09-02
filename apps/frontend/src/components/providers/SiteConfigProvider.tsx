'use client';

import { createContext, useContext } from 'react';
import { buildTimeSiteConfig, type RuntimeSiteConfig } from '@/config/site-config';

const SiteConfigContext = createContext<RuntimeSiteConfig>(buildTimeSiteConfig);

export function SiteConfigProvider({
  children,
  initialConfig = buildTimeSiteConfig,
}: {
  children: React.ReactNode;
  initialConfig?: RuntimeSiteConfig;
}) {
  return <SiteConfigContext.Provider value={initialConfig}>{children}</SiteConfigContext.Provider>;
}

export function useSiteConfig(): RuntimeSiteConfig {
  return useContext(SiteConfigContext);
}

export function useBranding(): RuntimeSiteConfig['branding'] {
  return useSiteConfig().branding;
}
