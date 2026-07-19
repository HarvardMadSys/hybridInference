'use client';

import { AuthProvider } from './AuthProvider';
import { QueryProvider } from './QueryProvider';
import { ToastProvider } from './ToastProvider';
import { SiteConfigProvider } from './SiteConfigProvider';

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <SiteConfigProvider>
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
