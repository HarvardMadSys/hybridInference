'use client';

import { UsageStats } from '@/components/features/dashboard/UsageStats';
import { UpdatesBanner } from '@/components/ui/UpdatesBanner';
import { UserAvatar } from '@/components/ui/UserAvatar';
import { useAuth } from '@/components/providers';
import { userDisplayName } from '@/lib/utils/userLabel';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';
import { roleLabel } from '@/lib/utils/roleLabel';

/**
 * The console's overview: who is signed in and how much of today's quota is
 * left. Keys, requests and models have their own pages (see `ConsoleNav`).
 */
export function DashboardView() {
  const { state } = useAuth();
  const displayName = userDisplayName(state.user);
  const t = useT();
  const notSet = t('dashboard.account.not_set', 'Not set');

  return (
    <div className="w-full space-y-8">
      {/* Same announcement banner as the landing page; dismissal is shared per-id. */}
      <UpdatesBanner />

      <div className="flex items-center gap-3.5">
        <UserAvatar user={state.user} size="lg" />
        <div>
          <h1 className="text-3xl font-bold text-gray-900">{t('dashboard.title', 'Dashboard')}</h1>
          <p className="mt-1 text-sm text-gray-600">
            {fill(t('dashboard.welcome', 'Welcome back, {name}'), { name: displayName })}
          </p>
        </div>
      </div>

      <div className="rounded-xl bg-white p-4 shadow-sm ring-1 ring-gray-200 sm:p-6">
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

      <UsageStats />
    </div>
  );
}
