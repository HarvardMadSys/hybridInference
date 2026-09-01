'use client';

import { AuthProvider } from './AuthProvider';
import { QueryProvider } from './QueryProvider';
import { ToastProvider } from './ToastProvider';
import { SiteConfigProvider } from './SiteConfigProvider';
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
          {children}
          <ToastProvider />
        </AuthProvider>
      </QueryProvider>
    </SiteConfigProvider>
  );
}

export { useAuth } from './AuthProvider';
