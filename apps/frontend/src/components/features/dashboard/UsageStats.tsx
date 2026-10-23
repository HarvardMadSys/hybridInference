'use client';

import { useMoney } from '@/lib/hooks/useMoney';
import { useState } from 'react';
import { useUsageStats } from '@/lib/hooks';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { fill } from '@/lib/utils/interpolate';

const RESET_AT_FORMAT: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  timeZoneName: 'short',
};

/** A tooltip on a statistic's label. */
function InfoTip({ children }: { children: React.ReactNode }): JSX.Element {
  return (
    <div className="group relative">
      <svg
        className="h-3.5 w-3.5 cursor-help text-gray-400"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
        />
      </svg>
      <div className="invisible absolute bottom-full left-1/2 z-10 mb-2 w-64 -translate-x-1/2 rounded-md bg-gray-900 px-3 py-2 text-xs text-white shadow-lg group-hover:visible">
        {children}
        <div className="absolute left-1/2 top-full -mt-1 -translate-x-1/2 border-4 border-transparent border-t-gray-900" />
      </div>
    </div>
  );
}

export function UsageStats(): JSX.Element {
  const money = useMoney();
  const branding = useBranding();
  const [period, setPeriod] = useState<'today' | 'month' | 'all'>('today');
  const { data: stats, isLoading, error } = useUsageStats(period);
  const quota = stats?.quota;
  const contactEmail = quota?.contact_email ?? branding.contactEmail;
  const t = useT();
  const fmt = useFormatter();

  return (
    <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-base sm:text-lg font-semibold tracking-tight text-gray-900">
          {t('dashboard.usage.title', 'Usage Statistics')}
        </h2>
        <select
          value={period}
          onChange={(e) => setPeriod(e.target.value as 'today' | 'month' | 'all')}
          className="rounded-md border-gray-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-blue-600 focus:outline-none"
        >
          <option value="today">{t('dashboard.usage.period_today', 'Today')}</option>
          <option value="month">{t('dashboard.usage.period_month', 'This Month')}</option>
          <option value="all">{t('dashboard.usage.period_all', 'All Time')}</option>
        </select>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-4 py-3 text-red-700 ring-1 ring-inset ring-red-200">
          {t(
            'dashboard.usage.load_failed',
            'Failed to load usage statistics. Please try again later.',
          )}
        </div>
      )}

      {isLoading && (
        <div className="flex justify-center py-8">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
        </div>
      )}

      {!isLoading && stats && (
        <div className="space-y-6">
          {stats.quota && !stats.quota.has_key && (
            <div className="rounded-md bg-red-50 px-4 py-3 text-red-700 ring-1 ring-inset ring-red-200">
              {t('dashboard.usage.no_key', 'API key not found')}
            </div>
          )}

          {quota?.has_key && (
            <div className="rounded-lg bg-blue-50 p-4 ring-1 ring-inset ring-blue-200">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <div className="text-xs font-medium uppercase tracking-wide text-blue-700">
                    {t('dashboard.usage.quota_remaining', 'Daily quota remaining')}
                  </div>
                  <div className="mt-1 text-2xl font-semibold tabular-nums text-blue-950">
                    {money.value(quota, 'remaining_today')}
                    {money.amount(quota, 'daily_limit') != null && (
                      <span className="ml-2 text-sm font-normal text-blue-700">
                        {fill(t('dashboard.usage.quota_of', 'of {limit}'), {
                          limit: money.value(quota, 'daily_limit'),
                        })}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 text-sm text-blue-800">
                    {quota.reset_at
                      ? fill(t('dashboard.usage.resets_at', 'Resets at {time}.'), {
                          time: fmt.dateTime(quota.reset_at, RESET_AT_FORMAT),
                        })
                      : t('dashboard.usage.resets_next', 'Resets at the next daily reset.')}
                  </div>
                  {quota.max_concurrency != null && (
                    <div className="mt-1 text-sm text-blue-800">
                      {fill(
                        t('dashboard.usage.max_concurrency', 'Max concurrent requests: {limit}'),
                        {
                          limit:
                            quota.max_concurrency === 0
                              ? t('dashboard.usage.unlimited', 'Unlimited')
                              : fmt.number(quota.max_concurrency),
                        },
                      )}
                    </div>
                  )}
                </div>
                {contactEmail && (
                  <div className="max-w-md text-sm text-blue-800">
                    {t('dashboard.usage.more_quota_lead', 'Need more quota? Email')}{' '}
                    <a className="font-medium underline" href={`mailto:${contactEmail}`}>
                      {contactEmail}
                    </a>{' '}
                    {t('dashboard.usage.more_quota_tail', 'and explain your use case.')}
                  </div>
                )}
              </div>
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {/* Estimated cost */}
            <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
              <div className="mb-1 flex items-center gap-1 text-xs text-gray-500">
                <span>{t('dashboard.usage.cost', 'Estimated cost')}</span>
                <InfoTip>
                  {t(
                    'dashboard.usage.cost_help',
                    'Internal cost attributed from logged usage. Today and This Month start at 00:00 UTC, when the daily quota resets. Not a bill — for transparency only.',
                  )}
                </InfoTip>
              </div>
              <div className="text-2xl font-semibold tabular-nums text-gray-900">
                {money.value(stats.usage, 'cost')}
              </div>
            </div>

            {/* Requests */}
            <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
              <div className="flex items-center gap-1 text-xs text-gray-500 mb-1">
                <span>{t('dashboard.usage.requests', 'Requests')}</span>
                <InfoTip>
                  {t(
                    'dashboard.usage.requests_help',
                    'Number of API requests (including errors). Retries count as separate requests.',
                  )}
                </InfoTip>
              </div>
              <div className="text-2xl font-semibold text-gray-900">
                {fmt.number(stats.usage.requests ?? 0)}
              </div>
            </div>

            {/* Input Tokens */}
            <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
              <div className="flex items-center gap-1 text-xs text-gray-500 mb-1">
                <span>{t('dashboard.usage.input_tokens', 'Input Tokens')}</span>
                <InfoTip>
                  {t(
                    'dashboard.usage.input_tokens_help',
                    'Tokens in your prompts and tool inputs sent to the model.',
                  )}
                </InfoTip>
              </div>
              <div className="text-2xl font-semibold text-gray-900">
                {fmt.number(stats.usage.prompt_tokens ?? 0)}
              </div>
            </div>

            {/* Output Tokens */}
            <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
              <div className="flex items-center gap-1 text-xs text-gray-500 mb-1">
                <span>{t('dashboard.usage.output_tokens', 'Output Tokens')}</span>
                <InfoTip>
                  {t(
                    'dashboard.usage.output_tokens_help',
                    'Tokens generated by the model in responses.',
                  )}
                </InfoTip>
              </div>
              <div className="text-2xl font-semibold text-gray-900">
                {fmt.number(stats.usage.completion_tokens ?? 0)}
              </div>
            </div>
          </div>

          <div className="text-center text-sm text-gray-500">
            {t(
              'dashboard.usage.footnote',
              'All prompts and responses are logged and may be open-sourced after anonymization. Free within plan limits; amounts are attribution from logs, not invoices.',
            )}
          </div>
        </div>
      )}
    </div>
  );
}
