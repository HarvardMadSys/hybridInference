'use client';

import { Sparkline } from './Sparkline';
import { isAnomalous } from './lib/anomaly';
import type { CostHistoryPoint, Density, UserRow as User } from './types';

interface UserRowProps {
  user: User;
  history: CostHistoryPoint[] | undefined; // 7d
  pageMedianToday: number;
  density: Density;
  expanded: boolean;
  onToggleExpanded: () => void;
  onApprove: () => void;
  onReject: () => void;
  onRegenerateKey: () => void;
}

const STATUS_GLYPH: Record<string, { glyph: string; color: string; title: string }> = {
  pending_approval: { glyph: '⬤', color: 'text-indigo-500', title: 'Pending' },
  active: { glyph: '●', color: 'text-gray-300', title: 'Active' },
  suspended: { glyph: '▲', color: 'text-amber-500', title: 'Suspended' },
  rejected: { glyph: '✕', color: 'text-red-500', title: 'Rejected' },
  deleted: { glyph: '✕', color: 'text-gray-400', title: 'Deleted' },
};

function todayCostBucket(cost: number, median: number): string {
  if (median <= 0 || cost < median) return '';
  const ratio = cost / median;
  if (ratio < 3) return 'bg-yellow-50';
  if (ratio < 10) return 'bg-amber-100';
  return 'bg-red-100';
}

export function UserRow({
  user,
  history,
  pageMedianToday,
  density,
  expanded,
  onToggleExpanded,
  onApprove,
  onReject,
  onRegenerateKey,
}: UserRowProps) {
  const statusMark = STATUS_GLYPH[user.status] ?? STATUS_GLYPH.active;
  const today = Number(user.usage_today_usd);
  const prior = (history ?? []).slice(0, 7).map((p) => Number(p.cost_usd));
  const anomalous = isAnomalous(today, prior);
  const nearQuota = false; // computed in summary; no per-row quota lookup here for now
  const badge = anomalous ? '⚠' : nearQuota ? '◐' : null;
  const rowHeight = density === 'compact' ? 'h-9' : 'h-14';
  const showSparkline = density === 'comfortable';
  const cellBucket = todayCostBucket(today, pageMedianToday);

  return (
    <>
      <tr
        onClick={onToggleExpanded}
        className={`${rowHeight} cursor-pointer border-b hover:bg-gray-50`}
      >
        <td className="px-2 text-center">
          <span className={statusMark.color} title={statusMark.title}>
            {statusMark.glyph}
          </span>
        </td>
        <td className="px-2">
          <div className="font-medium text-gray-900">{user.email}</div>
          {density === 'comfortable' && user.user_name && (
            <div className="text-xs text-gray-500">{user.user_name}</div>
          )}
        </td>
        <td className="px-2 text-xs uppercase text-gray-600">
          {user.role !== 'free' ? user.role : null}
        </td>
        <td className={`px-2 font-mono text-sm ${cellBucket}`}>${today.toFixed(2)}</td>
        {showSparkline && (
          <td className="px-2">
            <Sparkline points={history ?? []} />
          </td>
        )}
        <td className="px-2 font-mono text-sm">${Number(user.usage_month_usd).toFixed(2)}</td>
        <td className="px-2 font-mono text-sm text-gray-600">
          ${Number(user.usage_alltime_usd).toFixed(2)}
        </td>
        <td className="px-2 text-xs">{user.status.replace('_', ' ')}</td>
        <td className="px-2 text-center">
          {badge && (
            <span
              className={`inline-block rounded-full px-1 text-xs ${
                anomalous ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'
              }`}
              title={anomalous ? 'Anomaly: ≥5x 7d avg' : 'Near or over quota'}
            >
              {badge}
            </span>
          )}
        </td>
        <td className="px-2">
          {user.status === 'pending_approval' && (
            <div className="flex gap-1">
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onApprove();
                }}
                className="rounded bg-emerald-600 px-2 py-0.5 text-xs text-white"
              >
                Approve
              </button>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onReject();
                }}
                className="rounded bg-red-600 px-2 py-0.5 text-xs text-white"
              >
                Reject
              </button>
            </div>
          )}
          {user.status === 'active' && user.has_key && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onRegenerateKey();
              }}
              className="rounded bg-gray-200 px-2 py-0.5 text-xs"
            >
              Regenerate
            </button>
          )}
        </td>
      </tr>
      {expanded && (
        <tr className="bg-gray-50">
          <td colSpan={showSparkline ? 10 : 9} className="px-4 py-3">
            {/* UserDetailPanel slot - rendered by UserTable */}
            <span data-detail-slot={user.id} />
          </td>
        </tr>
      )}
    </>
  );
}
