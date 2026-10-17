'use client';

import { AuthProvider } from './AuthProvider';
import { LocaleProvider } from './LocaleProvider';
import { LocaleAccountSync } from './LocaleAccountSync';
import { QueryProvider } from './QueryProvider';
import { ToastProvider } from './ToastProvider';
import { SiteConfigProvider } from './SiteConfigProvider';
import { SetupGate } from '@/components/features/setup/SetupGate';
import type { RuntimeSiteConfig } from '@/config/site-config';
import { SOURCE_LOCALE, type Locale } from '@/lib/i18n/locales';
import type { Catalog } from '@/lib/i18n/translate';

export function Providers({
  children,
  initialSiteConfig,
  locale = SOURCE_LOCALE,
  catalog,
  moduleLocale = '',
}: {
  children: React.ReactNode;
  initialSiteConfig: RuntimeSiteConfig;
  /** The console's language for this request, resolved by the root layout. */
  locale?: Locale;
  /** That language's catalog; absent for English. */
  catalog?: Catalog;
  /** The compiled-in module's server `locale`. */
  moduleLocale?: string;
}) {
  return (
    <SiteConfigProvider initialConfig={initialSiteConfig}>
      <LocaleProvider locale={locale} catalog={catalog} moduleLocale={moduleLocale}>
        <QueryProvider>
          <AuthProvider>
            <LocaleAccountSync />
            {/* Above every route, including those a Site UI module renders, so
                a deployment without an administrator opens on /setup. */}
            <SetupGate>{children}</SetupGate>
            <ToastProvider />
          </AuthProvider>
        </QueryProvider>
      </LocaleProvider>
    </SiteConfigProvider>
  );
}

export { useAuth } from './AuthProvider';
