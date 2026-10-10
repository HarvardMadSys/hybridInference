'use client';

import { useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';

import { useSiteConfig } from '@/components/providers/SiteConfigProvider';

export const SETUP_PATH = '/setup';

export function isSetupPath(pathname: string | null | undefined): boolean {
  return (pathname ?? '').replace(/\/+$/, '') === SETUP_PATH;
}

/**
 * While the deployment has no administrator, every route but `/setup` sends
 * the visitor there.
 *
 * The decision is the server-rendered site configuration's `setup.required`,
 * read once per full page load. That is why setup ends with a full-page
 * navigation: a client-side one would keep this stale value and bounce straight
 * back. The page itself is not rendered in the meantime — a landing page or a
 * dashboard that flashed up and fired its requests would only fail.
 *
 * This is a convenience, not the protection: while setup is pending the backend
 * refuses sign-ups, and only the setup code creates the first account.
 */
export function SetupGate({ children }: { children: React.ReactNode }) {
  const { setup } = useSiteConfig();
  const pathname = usePathname();
  const router = useRouter();
  // `?.`: a console must tolerate a configuration object built before the key
  // existed (a test double, or a provider fed an older shape).
  const redirecting = setup?.required === true && !isSetupPath(pathname);

  useEffect(() => {
    if (redirecting) router.replace(SETUP_PATH);
  }, [redirecting, router]);

  if (redirecting) {
    return (
      <div
        className="flex min-h-screen items-center justify-center"
        role="status"
        aria-live="polite"
      >
        <div
          className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"
          aria-hidden
        />
        <span className="sr-only">Opening first-run setup…</span>
      </div>
    );
  }

  return <>{children}</>;
}
