'use client';

import { useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { OrganizationTabNav } from '@/components/features/organization/OrganizationTabNav';
import { OrganizationPeriodProvider } from '@/components/features/organization/parts';
import { useAuth } from '@/components/providers';
import { useT } from '@/components/providers/useT';
import { useOrganization } from '@/lib/hooks';

/**
 * The organization console: for an administrator of the account's own
 * organization.
 *
 * It has the admin area's frame (the whole width, the way back, a row of
 * tabs), and unlike that area it follows the reader's language, because it is
 * for an organization's own staff. The gateway decides who may read each
 * page; this guard only chooses what to show, from the account's profile.
 */
export default function OrganizationLayout({ children }: { children: React.ReactNode }) {
  const { state, refreshUser } = useAuth();
  const router = useRouter();
  const t = useT();
  const organization = state.user?.organization;
  const allowed = organization?.role === 'admin';

  // The profile has the name of the organization from when the page loaded.
  // After a rename by the platform administrator, the organization as it is
  // read now has the new name. The header shows that one, and the profile is
  // read again, because the navigation entry and the audit log take the name
  // from it. Once for a name: a profile that comes back with the old name is
  // not asked for again and again.
  const currentName = useOrganization(allowed).data?.name;
  const profileName = organization?.name;
  useEffect(() => {
    if (currentName !== undefined && profileName !== undefined && currentName !== profileName) {
      void refreshUser();
    }
  }, [currentName, profileName, refreshUser]);

  useEffect(() => {
    if (!state.loading && state.isAuthenticated && !allowed) {
      router.replace('/dashboard');
    }
  }, [state.loading, state.isAuthenticated, allowed, router]);

  return (
    <ProtectedRoute>
      {!allowed || !organization ? (
        <div className="flex min-h-[50vh] flex-col items-center justify-center text-center">
          <h1 className="text-lg font-semibold text-gray-900">
            {t('organization.guard.title', 'Organization administrator access required')}
          </h1>
          <p className="mt-1 text-sm text-gray-500">
            {t('organization.guard.body', "You don't have permission to view this page.")}
          </p>
          <Link
            href="/dashboard"
            className="mt-5 text-sm font-medium text-gray-900 underline decoration-gray-300 underline-offset-4 transition hover:decoration-gray-900"
          >
            {t('organization.guard.back', 'Back to Dashboard')}
          </Link>
        </div>
      ) : (
        <div className="w-full pb-20">
          {/* One short line of chrome, as in the admin area: where you are and
              the way back, so a page's own content starts near the top. */}
          <header className="flex min-w-0 items-center gap-2">
            <Link
              href="/dashboard"
              className="group flex shrink-0 items-center gap-1 rounded text-[13px] text-gray-500 transition hover:text-gray-900"
            >
              <svg
                className="h-3.5 w-3.5 transition group-hover:-translate-x-px"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2.5}
                aria-hidden="true"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18"
                />
              </svg>
              {t('organization.shell.dashboard', 'Dashboard')}
            </Link>
            <span aria-hidden="true" className="text-[13px] text-gray-300">
              /
            </span>
            <h1 className="truncate text-[18px] font-semibold tracking-tight text-gray-900">
              {currentName ?? organization.name}
            </h1>
          </header>

          <OrganizationTabNav />

          <OrganizationPeriodProvider>
            <div className="mt-4">{children}</div>
          </OrganizationPeriodProvider>
        </div>
      )}
    </ProtectedRoute>
  );
}
