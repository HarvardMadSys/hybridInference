'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { ADMIN_SETTINGS_PATH } from '@/components/features/configuration/paths';
import { isSetupPath } from '@/components/features/setup/SetupGate';
import { useAuth } from '@/components/providers';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { publicRouteFor } from '@/site-ui/routes';
import { useConsoleFrame, useConsoleWidth } from '@/components/ui/consoleWidth';
import { useT } from '@/components/providers/useT';

/**
 * "Required settings are missing", in the console chrome.
 *
 * Only for signed-in users, and only on console routes. An administrator is
 * told where to fix it; anyone else is told whom to ask, since some features
 * will not work for them. Anonymous visitors and the public pages (the
 * landing page, sign-in, sign-up) do not get it: they can do nothing about it,
 * and a stranger has no use for a deployment's housekeeping. Nor do `/setup`
 * and Admin → Settings, which list the missing settings themselves.
 *
 * The flag is the server-rendered site configuration's, so a save on the
 * Settings tab calls `router.refresh()` to update it.
 */
export function ConfigurationBanner(): JSX.Element | null {
  const { setup, configuration } = useSiteConfig();
  const pathname = usePathname() ?? '/';

  // `?.`: tolerate a configuration object built before these keys existed.
  if (configuration?.incomplete !== true || setup?.required === true) return null;
  if (isSetupPath(pathname) || publicRouteFor(pathname) !== null) return null;
  if (pathname === ADMIN_SETTINGS_PATH || pathname.startsWith(`${ADMIN_SETTINGS_PATH}/`)) {
    return null;
  }
  // Only now is the session needed, so a complete deployment renders the
  // chrome without reading it.
  return <MissingConfigurationNotice />;
}

function MissingConfigurationNotice(): JSX.Element | null {
  const { state } = useAuth();
  const consoleWidth = useConsoleWidth();
  const { bar } = useConsoleFrame();
  const t = useT();
  if (!state.isAuthenticated || !state.user) return null;

  // Under the dashboard's bar it is a strip of the same width; elsewhere a
  // card as wide as the page below it.
  return (
    <div className={bar ? 'w-full shrink-0' : `mx-auto w-full ${consoleWidth} px-4 sm:px-6`}>
      <div
        role="status"
        className={`w-full border-amber-200 bg-amber-50 text-sm text-amber-900 ${
          bar ? 'border-b px-4 py-2 sm:px-6' : 'rounded-2xl border px-4 py-3 shadow-subtle sm:px-5'
        }`}
      >
        {state.user.is_admin ? (
          <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="font-semibold">Ask admin to configure</span>
            {/* Filtered to the required settings that have no value, on every
                sub-tab; clearing the filter lands on Models, where an empty
                catalog -- the other way to be incomplete -- is fixed. */}
            <Link
              href={`${ADMIN_SETTINGS_PATH}?tab=models&missing=1`}
              prefetch={false}
              className="font-medium underline underline-offset-2"
            >
              Open Settings
            </Link>
          </p>
        ) : (
          <p>
            {t(
              'chrome.configuration.user_notice',
              'Ask admin to configure this service. Required configuration is missing.',
            )}
          </p>
        )}
      </div>
    </div>
  );
}
