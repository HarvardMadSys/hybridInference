'use client';

import { useState } from 'react';
import { useUsageStats } from '@/lib/hooks';

export function UsageStats(): JSX.Element {
  const [period, setPeriod] = useState<'today' | 'month' | 'all'>('today');
  const { data: stats, isLoading, error } = useUsageStats(period);

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
        <div className="space-y-8">
          {stats.quota && !stats.quota.has_key && (
            <div className="rounded-md bg-red-50 px-4 py-3 text-red-700 ring-1 ring-inset ring-red-200">
              API key not found
            </div>
          )}

          <div>
            <h3 className="mb-3 text-sm font-medium text-gray-700">Quota</h3>
            <div className="grid grid-cols-2 gap-4">
              <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
                <div className="text-xs text-gray-500">Daily Limit</div>
                <div className="text-lg font-semibold">
                  ${(stats.quota.daily_limit_usd ?? 0).toFixed(2)}
                </div>
              </div>
              <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
                <div className="text-xs text-gray-500">Spent Today</div>
                <div className="text-lg font-semibold">
                  ${(stats.quota.spent_today_usd ?? 0).toFixed(2)}
                </div>
              </div>
              <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
                <div className="text-xs text-gray-500">Spent This Month</div>
                <div className="text-lg font-semibold">
                  ${(stats.quota.spent_month_usd ?? 0).toFixed(2)}
                </div>
              </div>
              <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
                <div className="text-xs text-gray-500">Remaining Today</div>
                <div className="text-lg font-semibold text-green-600">
                  ${(stats.quota.remaining_today_usd ?? 0).toFixed(2)}
                </div>
              </div>
            </div>
          </div>

          <div>
            <h3 className="mb-3 text-sm font-medium text-gray-700">Usage</h3>
            <div className="grid grid-cols-2 gap-4">
              <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
                <div className="text-xs text-gray-500">Requests</div>
                <div className="text-lg font-semibold">
                  {(stats.usage.requests ?? 0).toLocaleString()}
                </div>
              </div>
              <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
                <div className="text-xs text-gray-500">Total Cost</div>
                <div className="text-lg font-semibold">
                  ${(stats.usage.cost_usd ?? 0).toFixed(4)}
                </div>
              </div>
              <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
                <div className="text-xs text-gray-500">Input Tokens</div>
                <div className="text-lg font-semibold">
                  {(stats.usage.prompt_tokens ?? 0).toLocaleString()}
                </div>
              </div>
              <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
                <div className="text-xs text-gray-500">Output Tokens</div>
                <div className="text-lg font-semibold">
                  {(stats.usage.completion_tokens ?? 0).toLocaleString()}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
