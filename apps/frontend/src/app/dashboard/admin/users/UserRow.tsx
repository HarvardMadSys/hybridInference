'use client';

import { Sparkline } from './Sparkline';
import { isAnomalous } from './lib/anomaly';
import type { UserTurnAverages } from '@/lib/api/admin';
import type { CostHistoryPoint, Density, UserRow as User } from './types';

interface UserRowProps {
  user: User;
  history: CostHistoryPoint[] | undefined; // 7d
  turns: UserTurnAverages | undefined;
  pageMedianToday: number;
  density: Density;
  expanded: boolean;
  onToggleExpanded: () => void;
  onApprove: () => void;
  onReject: () => void;
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
  turns,
  pageMedianToday,
  density,
  expanded,
  onToggleExpanded,
  onApprove,
  onReject,
}: UserRowProps) {
  const statusMark = STATUS_GLYPH[user.status] ?? STATUS_GLYPH.active;
  const today = Number(user.usage_today_usd);
  // Exclude today's data point: cost-history is returned ascending and includes
  // today. The anomaly rule compares today vs the *prior* 7-day average, so we
  // must drop the last point (today) before passing to isAnomalous.
  const todayIso = new Date().toISOString().slice(0, 10);
  const prior = (history ?? [])
    .filter((p) => p.day < todayIso)
    .slice(-7)
    .map((p) => Number(p.cost_usd));
  const anomalous = isAnomalous(today, prior);
  const badge = anomalous ? '⚠' : null;
  const rowHeight = density === 'compact' ? 'h-9' : 'h-14';
  const showSparkline = density === 'comfortable';
  const cellBucket = todayCostBucket(today, pageMedianToday);

  return (
    <tr
      onClick={onToggleExpanded}
      className={`${rowHeight} cursor-pointer border-b hover:bg-gray-50 ${expanded ? 'bg-gray-50' : ''}`}
    >
      <td className="px-2 text-center">
        <span className={statusMark.color} title={statusMark.title}>
          {statusMark.glyph}
        </span>
      </td>
      <td className="max-w-[12rem] px-2">
        <div className="truncate font-medium text-gray-900" title={user.email}>
          {user.email}
        </div>
        {density === 'comfortable' && user.user_name && (
          <div className="truncate text-xs text-gray-500" title={user.user_name}>
            {user.user_name}
          </div>
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
      <td className="px-2 font-mono text-sm text-gray-600 tabular-nums">
        {turns?.avg_turns != null ? turns.avg_turns.toFixed(1) : '—'}
      </td>
      <td className="px-2 font-mono text-sm text-gray-600 tabular-nums">
        {turns?.avg_user_turns != null ? turns.avg_user_turns.toFixed(1) : '—'}
      </td>
      <td className="px-2 text-xs">{user.status.replace('_', ' ')}</td>
      <td className="px-2 text-center">
        {badge && (
          <span
            className="inline-block rounded-full bg-red-100 px-1 text-xs text-red-700"
            title="Anomaly: ≥5x 7d avg"
          >
            {badge}
          </span>
        )}
      </td>
      <td className="px-2">
        {user.status === 'pending_approval' && (
          <div className="flex gap-1">
            <button
              onClick={(e) => {
                e.stopPropagation();
                onApprove();
              }}
              className="rounded bg-emerald-600 px-2 py-0.5 text-xs text-white"
            >
              Approve
            </button>
            <button
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
      </td>
    </tr>
  );
}
