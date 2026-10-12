'use client';

import { AuthProvider } from './AuthProvider';
import { QueryProvider } from './QueryProvider';
import { ToastProvider } from './ToastProvider';
import { SiteConfigProvider } from './SiteConfigProvider';
import { SetupGate } from '@/components/features/setup/SetupGate';
import type { RuntimeSiteConfig } from '@/config/site-config';

export function Providers({
  children,
  initialSiteConfig,
}: {
  children: React.ReactNode;
  initialSiteConfig: RuntimeSiteConfig;
}) {
  return (
    <SiteConfigProvider initialConfig={initialSiteConfig}>
      <QueryProvider>
        <AuthProvider>
          {/* Above every route, including those a Site UI module renders, so
              a deployment without an administrator opens on /setup. */}
          <SetupGate>{children}</SetupGate>
          <ToastProvider />
        </AuthProvider>
      </QueryProvider>
    </SiteConfigProvider>
  );
}

export { useAuth } from './AuthProvider';
