'use client';

import Link from 'next/link';
import { ApiKeyManager } from '@/components/features/dashboard/ApiKeyManager';
import { ModelsSection } from '@/components/features/dashboard/ModelsSection';
import { RecentRequests } from '@/components/features/dashboard/RecentRequests';
import { UsageStats } from '@/components/features/dashboard/UsageStats';
import { UpdatesBanner } from '@/components/ui/UpdatesBanner';
import { UserAvatar } from '@/components/ui/UserAvatar';
import { useAuth } from '@/components/providers';
import { hasRole } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useBatchCapabilities } from '@/lib/hooks/queries/useBatchCapabilities';
import { userDisplayName } from '@/lib/utils/userLabel';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';
import { roleLabel } from '@/lib/utils/roleLabel';

const TOOL_LINK =
  'inline-flex min-h-11 items-center justify-center rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-gray-800';

/**
 * The Agents link, naming the account this console tab is using.
 *
 * The Agent signs in on its own and may be using a different account of this
 * browser; given this one, it can say so and offer to switch rather than
 * silently showing another account's work.
 */
function agentsEntryUrl(agentsUrl: string, userId: string | undefined): string {
  if (!userId) return agentsUrl;
  const separator = agentsUrl.includes('?') ? '&' : '?';
  return `${agentsUrl}${separator}gateway_account=${encodeURIComponent(userId)}`;
}

export function DashboardView() {
  const { state } = useAuth();
  const { branding, features, agentsUrl } = useSiteConfig();
  const displayName = userDisplayName(state.user);
  const t = useT();
  const notSet = t('dashboard.account.not_set', 'Not set');
  const { capabilities: batch, error: batchError } = useBatchCapabilities();
  // Each tool is offered on its own condition; Batch history never brings
  // the other tools along.
  const isInternal = hasRole(state.user?.role, 'internal');
  const isAdmin = state.user?.role === 'admin';
  const showAgents = isInternal && features.agents;
  const showPlayground = isInternal;
  const showBatches = batch ? batch.show_entry : Boolean(batchError && isInternal);

  return (
    <div className="mx-auto w-full max-w-5xl space-y-8">
      {/* Same announcement banner as the landing page; dismissal is shared per-id. */}
      <UpdatesBanner />

      <div className="mb-8">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3.5">
            <UserAvatar user={state.user} size="lg" />
            <div>
              <h1 className="text-3xl font-bold text-gray-900">
                {t('dashboard.title', 'Dashboard')}
              </h1>
              <p className="mt-1 text-sm text-gray-600">
                {fill(t('dashboard.welcome', 'Welcome back, {name}'), { name: displayName })}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Link
              href="/dashboard/settings"
              className="inline-flex items-center rounded-md bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm ring-1 ring-inset ring-gray-300 hover:bg-gray-50"
            >
              <svg className="mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
                />
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
                />
              </svg>
              {t('dashboard.settings_link', 'Settings')}
            </Link>
            {branding.docsUrl && (
              <a
                href={branding.docsUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center rounded-md bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm ring-1 ring-inset ring-gray-300 hover:bg-gray-50"
              >
                <svg className="mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M18 13v6a2 2 0 01-2 2H6a2 2 0 01-2-2V8a2 2 0 012-2h6"
                  />
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M15 3h6m0 0v6m0-6L10 14"
                  />
                </svg>
                {t('dashboard.docs_link', 'Docs')}
              </a>
            )}
          </div>
        </div>
      </div>

      <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
        <h2 className="text-base sm:text-lg font-semibold tracking-tight text-gray-900 mb-2">
          {t('dashboard.account.title', 'Account Information')}
        </h2>
        <div className="space-y-3 text-sm">
          <div className="flex justify-between">
            <span className="text-gray-600">{t('dashboard.account.username', 'Nickname:')}</span>
            <span className="font-medium">{state.user?.user_name || notSet}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">{t('dashboard.account.email', 'Email:')}</span>
            <span className="font-medium">{state.user?.email || notSet}</span>
          </div>
          {state.user?.login_name && (
            <div className="flex justify-between">
              <span className="text-gray-600">
                {t('dashboard.account.login_name', 'Login name:')}
              </span>
              <span className="font-medium">{state.user.login_name}</span>
            </div>
          )}
          {state.user?.phone && (
            <div className="flex justify-between">
              <span className="text-gray-600">
                {t('dashboard.account.mobile', 'Mobile number:')}
              </span>
              <span className="font-medium">{state.user.phone}</span>
            </div>
          )}
          <div className="flex justify-between">
            <span className="text-gray-600">{t('dashboard.account.user_id', 'User ID:')}</span>
            <span className="font-mono text-xs">{state.user?.id}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">{t('dashboard.account.plan', 'Plan:')}</span>
            <span className="font-medium capitalize">{roleLabel(state.user?.role, t)}</span>
          </div>
        </div>
      </div>

      <ApiKeyManager />

      <ModelsSection />

      <UsageStats />

      <RecentRequests />

      {(showAgents || showPlayground || showBatches || isAdmin) && (
        <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
          <h2 className="text-base sm:text-lg font-semibold tracking-tight text-gray-900 mb-4">
            {t('dashboard.tools.title', 'Tools')}
          </h2>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {showAgents && (
              <a href={agentsEntryUrl(agentsUrl, state.user?.id)} className={TOOL_LINK}>
                <svg className="mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M7 3v12m0 0a3 3 0 1 0 3 3m-3-3a3 3 0 0 1 3 3m7-15a3 3 0 1 1-3 3m3-3v6a4 4 0 0 1-4 4H10"
                  />
                </svg>
                Agents
              </a>
            )}
            {showPlayground && (
              <Link href="/dashboard/playground" prefetch={false} className={TOOL_LINK}>
                <svg className="mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
                  />
                </svg>
                {t('dashboard.internal.playground', 'API Playground')}
              </Link>
            )}
            {showBatches && (
              <Link href="/dashboard/batches" prefetch={false} className={TOOL_LINK}>
                <svg className="mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M4 6h16M4 12h16M4 18h10"
                  />
                </svg>
                {t('dashboard.internal.batches', 'Batches')}
              </Link>
            )}
            {isAdmin && (
              <>
                <Link href="/dashboard/admin/users" className={TOOL_LINK}>
                  <svg
                    className="mr-2 h-4 w-4"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"
                    />
                  </svg>
                  {t('dashboard.internal.admin', 'Admin Console')}
                </Link>
                <a href="/pgadmin/" target="_blank" rel="noopener noreferrer" className={TOOL_LINK}>
                  <svg
                    className="mr-2 h-4 w-4"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4"
                    />
                  </svg>
                  pgAdmin
                </a>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
