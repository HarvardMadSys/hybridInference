'use client';

import { createContext, useContext, useEffect, useState } from 'react';
import {
  buildTimeSiteConfig,
  resolveRuntimeSiteConfig,
  type RuntimeSiteConfig,
} from '@/config/site-config';
import { config } from '@/config/env';

const SiteConfigContext = createContext<RuntimeSiteConfig>(buildTimeSiteConfig);

export function SiteConfigProvider({ children }: { children: React.ReactNode }) {
  const [siteConfig, setSiteConfig] = useState(buildTimeSiteConfig);

  useEffect(() => {
    const controller = new AbortController();
    async function loadSiteConfig() {
      try {
        const apiBase = config.apiBase.replace(/\/+$/, '');
        const response = await fetch(`${apiBase}/site-config`, {
          cache: 'no-store',
          signal: controller.signal,
        });
        if (!response.ok) return;
        setSiteConfig(resolveRuntimeSiteConfig(await response.json()));
      } catch (error) {
        if (!controller.signal.aborted) {
          console.warn('Unable to load runtime site config; using build-time defaults.', error);
        }
      }
    }
    void loadSiteConfig();
    return () => controller.abort();
  }, []);

  return <SiteConfigContext.Provider value={siteConfig}>{children}</SiteConfigContext.Provider>;
}

export function useSiteConfig(): RuntimeSiteConfig {
  return useContext(SiteConfigContext);
}

export function useBranding(): RuntimeSiteConfig['branding'] {
  return useSiteConfig().branding;
}
