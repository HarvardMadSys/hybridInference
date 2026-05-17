'use client';

import { useState } from 'react';
import { useUsageStats } from '@/lib/hooks';

function formatUsd(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(amount);
}

function formatResetAt(value?: string | null): string {
  if (!value) return 'the next daily reset';
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(new Date(value));
}

export function UsageStats(): JSX.Element {
  const [period, setPeriod] = useState<'today' | 'month' | 'all'>('today');
  const { data: stats, isLoading, error } = useUsageStats(period);
  const quota = stats?.quota;
  const contactEmail = quota?.contact_email ?? 'admin@freeinference.org';

  return (
    <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-base sm:text-lg font-semibold tracking-tight text-gray-900">
          Usage Statistics
        </h2>
        <select
          value={period}
          onChange={(e) => setPeriod(e.target.value as 'today' | 'month' | 'all')}
          className="rounded-md border-gray-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-blue-600 focus:outline-none"
        >
          <option value="today">Today</option>
          <option value="month">This Month</option>
          <option value="all">All Time</option>
        </select>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-4 py-3 text-red-700 ring-1 ring-inset ring-red-200">
          Failed to load usage statistics. Please try again later.
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
              API key not found
            </div>
          )}

          {quota?.has_key && (
            <div className="rounded-lg bg-blue-50 p-4 ring-1 ring-inset ring-blue-200">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <div className="text-xs font-medium uppercase tracking-wide text-blue-700">
                    Daily quota remaining
                  </div>
                  <div className="mt-1 text-2xl font-semibold tabular-nums text-blue-950">
                    {formatUsd(quota.remaining_today_usd ?? 0)}
                    {quota.daily_limit_usd != null && (
                      <span className="ml-2 text-sm font-normal text-blue-700">
                        of {formatUsd(quota.daily_limit_usd)}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 text-sm text-blue-800">
                    Resets at {formatResetAt(quota.reset_at)}.
                  </div>
                  {quota.max_concurrency != null && (
                    <div className="mt-1 text-sm text-blue-800">
                      Max concurrent requests: {quota.max_concurrency}
                    </div>
                  )}
                </div>
                <div className="max-w-md text-sm text-blue-800">
                  Need more quota? Email{' '}
                  <a className="font-medium underline" href={`mailto:${contactEmail}`}>
                    {contactEmail}
                  </a>{' '}
                  and explain your use case.
                </div>
              </div>
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {/* Estimated cost */}
            <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
              <div className="mb-1 flex items-center gap-1 text-xs text-gray-500">
                <span>Estimated cost</span>
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
                    Internal cost attributed from logged usage ({period} window, UTC boundary
                    follows your timezone). Not a bill — for transparency only.
                    <div className="absolute left-1/2 top-full -mt-1 -translate-x-1/2 border-4 border-transparent border-t-gray-900" />
                  </div>
                </div>
              </div>
              <div className="text-2xl font-semibold tabular-nums text-gray-900">
                {formatUsd(stats.usage.cost_usd ?? 0)}
              </div>
            </div>

            {/* Requests */}
            <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
              <div className="flex items-center gap-1 text-xs text-gray-500 mb-1">
                <span>Requests</span>
                <div className="group relative">
                  <svg
                    className="h-3.5 w-3.5 text-gray-400 cursor-help"
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
                  <div className="invisible group-hover:visible absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 rounded-md bg-gray-900 px-3 py-2 text-xs text-white shadow-lg z-10">
                    Number of API requests (including errors). Retries count as separate requests.
                    <div className="absolute top-full left-1/2 -translate-x-1/2 -mt-1 border-4 border-transparent border-t-gray-900"></div>
                  </div>
                </div>
              </div>
              <div className="text-2xl font-semibold text-gray-900">
                {(stats.usage.requests ?? 0).toLocaleString()}
              </div>
            </div>

            {/* Input Tokens */}
            <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
              <div className="flex items-center gap-1 text-xs text-gray-500 mb-1">
                <span>Input Tokens</span>
                <div className="group relative">
                  <svg
                    className="h-3.5 w-3.5 text-gray-400 cursor-help"
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
                  <div className="invisible group-hover:visible absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 rounded-md bg-gray-900 px-3 py-2 text-xs text-white shadow-lg z-10">
                    Tokens in your prompts and tool inputs sent to the model.
                    <div className="absolute top-full left-1/2 -translate-x-1/2 -mt-1 border-4 border-transparent border-t-gray-900"></div>
                  </div>
                </div>
              </div>
              <div className="text-2xl font-semibold text-gray-900">
                {(stats.usage.prompt_tokens ?? 0).toLocaleString()}
              </div>
            </div>

            {/* Output Tokens */}
            <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
              <div className="flex items-center gap-1 text-xs text-gray-500 mb-1">
                <span>Output Tokens</span>
                <div className="group relative">
                  <svg
                    className="h-3.5 w-3.5 text-gray-400 cursor-help"
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
                  <div className="invisible group-hover:visible absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 rounded-md bg-gray-900 px-3 py-2 text-xs text-white shadow-lg z-10">
                    Tokens generated by the model in responses.
                    <div className="absolute top-full left-1/2 -translate-x-1/2 -mt-1 border-4 border-transparent border-t-gray-900"></div>
                  </div>
                </div>
              </div>
              <div className="text-2xl font-semibold text-gray-900">
                {(stats.usage.completion_tokens ?? 0).toLocaleString()}
              </div>
            </div>
          </div>

          <div className="text-center text-sm text-gray-500">
            All prompts and responses are logged. Free within plan limits; dollar amounts are
            attribution from logs, not invoices.
          </div>
        </div>
      )}
    </div>
  );
}
