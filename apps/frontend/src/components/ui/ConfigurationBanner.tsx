'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { CONFIGURATION_TAB_PATH } from '@/components/features/configuration/paths';
import { isSetupPath } from '@/components/features/setup/SetupGate';
import { useAuth } from '@/components/providers';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { publicRouteFor } from '@/site-ui/routes';

/**
 * "Required settings are missing", in the console chrome.
 *
 * Only for signed-in users, and only on console routes. An administrator is
 * told where to fix it; anyone else is told whom to ask, since some features
 * will not work for them. Anonymous visitors and the public pages (the
 * landing page, sign-in, sign-up) do not get it: they can do nothing about it,
 * and a stranger has no use for a deployment's housekeeping. Nor do `/setup`
 * and the Configuration tab, which list the missing settings themselves.
 *
 * The flag is the server-rendered site configuration's, so a save on the
 * Configuration tab calls `router.refresh()` to update it.
 */
export function ConfigurationBanner(): JSX.Element | null {
  const { setup, configuration } = useSiteConfig();
  const pathname = usePathname() ?? '/';

  // `?.`: tolerate a configuration object built before these keys existed.
  if (configuration?.incomplete !== true || setup?.required === true) return null;
  if (isSetupPath(pathname) || publicRouteFor(pathname) !== null) return null;
  if (pathname === CONFIGURATION_TAB_PATH || pathname.startsWith(`${CONFIGURATION_TAB_PATH}/`)) {
    return null;
  }
  // Only now is the session needed, so a complete deployment renders the
  // chrome without reading it.
  return <MissingConfigurationNotice />;
}

function MissingConfigurationNotice(): JSX.Element | null {
  const { state } = useAuth();
  if (!state.isAuthenticated || !state.user) return null;

  return (
    <div className="mx-auto w-full max-w-5xl px-6">
      <div
        role="status"
        className="w-full rounded-2xl border border-amber-200 bg-amber-50 px-5 py-3 text-sm text-amber-900 shadow-subtle"
      >
        {state.user.is_admin ? (
          <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="font-semibold">Required settings are missing.</span>
            <Link
              href={`${CONFIGURATION_TAB_PATH}?missing=1`}
              prefetch={false}
              className="font-medium underline underline-offset-2"
            >
              Open Configuration
            </Link>
          </p>
        ) : (
          <p>
            This service is missing required configuration, so some features may not work. Please
            contact your administrator.
          </p>
        )}
      </div>
    </div>
  );
}
