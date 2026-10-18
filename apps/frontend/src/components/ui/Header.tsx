'use client';

import Link from 'next/link';
import { useAuth } from '@/components/providers';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';
import { AccountMenu } from './AccountMenu';
import { LanguageSwitcher } from './LanguageSwitcher';
import { useConsoleWidth } from '@/components/ui/consoleWidth';

export function Header() {
  const consoleWidth = useConsoleWidth();
  const { state } = useAuth();
  const { branding, features } = useSiteConfig();
  const t = useT();

  return (
    <header
      className={`mx-auto flex w-full ${consoleWidth} flex-wrap items-center justify-between gap-3 px-6 py-6`}
    >
      <div className="flex min-w-0 items-baseline gap-2">
        <Link
          href="/"
          prefetch={false}
          className="inline-flex min-w-0 items-center gap-2 text-xl font-bold tracking-tight"
        >
          {branding.logoUrl ? (
            // Runtime assets cannot use next/image because their origin is
            // deliberately unknown when the neutral image is built.
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={branding.logoUrl}
              alt={fill(t('chrome.header.logo_alt', '{app_name} logo'), {
                app_name: branding.appName,
              })}
              className="h-8 w-auto shrink-0 object-contain"
            />
          ) : null}
          {branding.appName}
        </Link>
        {branding.orgName && branding.orgUrl && (
          <a
            href={branding.orgUrl}
            className="font-serif text-sm text-gray-500 hover:text-crimson"
            target="_blank"
            rel="noopener noreferrer"
          >
            {branding.orgName}
          </a>
        )}
      </div>
      <div className="ml-auto flex flex-wrap items-center justify-end gap-1 sm:gap-2">
        {branding.statusUrl && (
          <a
            href={branding.statusUrl}
            className="rounded-md px-2 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 sm:px-3"
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('chrome.header.status', 'Status')}
          </a>
        )}
        {features.publicStats && (
          <Link
            href="/stats"
            prefetch={false}
            className="rounded-md px-2 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 sm:px-3"
          >
            {t('chrome.header.stats', 'Stats')}
          </Link>
        )}
        {branding.navLinks.map((link) => (
          <a
            key={link.url}
            href={link.url}
            className="rounded-md px-2 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 sm:px-3"
            target="_blank"
            rel="noopener noreferrer"
          >
            {link.label}
          </a>
        ))}
        {!state.isAuthenticated && <LanguageSwitcher />}
        {state.isAuthenticated && (
          <>
            {features.rag && (
              <Link
                href="/chat"
                prefetch={false}
                className="rounded-md px-2 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 sm:px-3"
              >
                {t('chrome.header.docs_assistant', 'Docs Assistant')}
              </Link>
            )}
            <Link
              href="/dashboard"
              className="rounded-md px-2 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 sm:px-3"
            >
              {t('chrome.header.dashboard', 'Dashboard')}
            </Link>
            <AccountMenu />
          </>
        )}
      </div>
    </header>
  );
}
