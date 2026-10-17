'use client';

import { Sparkline } from './Sparkline';
import { isAnomalous } from './lib/anomaly';
import { bandStyle } from './lib/automation';
import { ROLE_STYLE, STATUS_STYLE } from './ui';
import { NoteIcon } from './UserDetailPanel';
import type {
  UserAskQuestionFraction,
  UserAutomationScore,
  UserTurnAverages,
} from '@/lib/api/admin';
import type { CostHistoryPoint, Density, UserRow as User } from './types';
import type { ColumnId } from './lib/columns';
import { userAccountLabel } from '@/lib/utils/userLabel';

interface UserRowProps {
  user: User;
  history: CostHistoryPoint[] | undefined; // 7d
  turns: UserTurnAverages | undefined;
  askQuestion: UserAskQuestionFraction | undefined;
  automation: UserAutomationScore | undefined;
  scoreState: 'idle' | 'loading' | 'loaded';
  pageMedianToday: number;
  density: Density;
  /** Metric columns to render, and the ones that start a column group. */
  visible: Set<ColumnId>;
  groupStarts: Set<ColumnId>;
  expanded: boolean;
  onToggleExpanded: () => void;
  onApprove: () => void;
  onReject: () => void;
}

// Token counts run large (millions–billions for heavy users); render them
// compactly so the column stays narrow. Intl handles clean rounding (1000 → 1K,
// not 1.0K) and localization. Request counts are smaller and use a plain
// grouped number instead.
const compactTokenFormatter = new Intl.NumberFormat('en', {
  notation: 'compact',
  minimumFractionDigits: 0,
  maximumFractionDigits: 1,
});

function formatCompact(n: number): string {
  return compactTokenFormatter.format(n);
}

function todayCostBucket(cost: number, median: number): string {
  if (median <= 0 || cost < median) return '';
  const ratio = cost / median;
  if (ratio < 3) return 'bg-yellow-50';
  if (ratio < 10) return 'bg-amber-100';
  return 'bg-red-100';
}

// Numeric cells: right-aligned tabular figures; a zero is dimmed so the eye
// lands on the users that actually have activity.
const NUM = 'whitespace-nowrap px-2.5 text-right text-[13px] tabular-nums';
const numTone = (n: number) => (n > 0 ? 'text-gray-900' : 'text-gray-300');

export function UserRow({
  user,
  history,
  turns,
  askQuestion,
  automation,
  scoreState,
  pageMedianToday,
  density,
  visible,
  groupStarts,
  expanded,
  onToggleExpanded,
  onApprove,
  onReject,
}: UserRowProps) {
  const status = STATUS_STYLE[user.status] ?? STATUS_STYLE.active;
  const today = Number(user.usage_today_usd);
  const month = Number(user.usage_month_usd);
  const alltime = Number(user.usage_alltime_usd);
  // Exclude today's data point: cost-history is returned ascending and includes
  // today. The anomaly rule compares today vs the *prior* 7-day average, so we
  // must drop the last point (today) before passing to isAnomalous.
  const todayIso = new Date().toISOString().slice(0, 10);
  const prior = (history ?? [])
    .filter((p) => p.day < todayIso)
    .slice(-7)
    .map((p) => Number(p.cost_usd));
  const anomalous = isAnomalous(today, prior);
  const compact = density === 'compact';
  const rowHeight = compact ? 'h-10' : 'h-14';
  const edge = (id: ColumnId) => (groupStarts.has(id) ? 'border-l border-gray-100' : '');
  const cellBucket = todayCostBucket(today, pageMedianToday);
  const label = userAccountLabel(user);
  // The identity cell is sticky so it stays in view when the table scrolls
  // sideways; it needs an opaque background that follows the row's state.
  const stickyBg = expanded ? 'bg-gray-50' : 'bg-white group-hover:bg-gray-50';

  return (
    <tr
      onClick={onToggleExpanded}
      aria-expanded={expanded}
      className={`${rowHeight} group cursor-pointer border-b border-gray-100 transition-colors hover:bg-gray-50 ${expanded ? 'bg-gray-50' : ''}`}
    >
      <td className={`sticky left-0 z-10 max-w-[12.5rem] py-1.5 pl-3 pr-2.5 ${stickyBg}`}>
        <div className="flex items-center gap-2">
          <svg
            className={`h-3.5 w-3.5 shrink-0 text-gray-300 transition-transform group-hover:text-gray-500 ${expanded ? 'rotate-90 text-gray-500' : ''}`}
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
              clipRule="evenodd"
            />
          </svg>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="truncate text-[13px] font-medium text-gray-900" title={label}>
                {label}
              </span>
              {user.role !== 'free' && (
                <span
                  className={`shrink-0 rounded px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide ${ROLE_STYLE[user.role] ?? ROLE_STYLE.free}`}
                >
                  {user.role}
                </span>
              )}
              {user.admin_note && (
                <span
                  className="inline-flex shrink-0 text-amber-500"
                  title={`Admin note: ${user.admin_note}`}
                  aria-label="Has admin note"
                >
                  <NoteIcon className="h-3.5 w-3.5" />
                </span>
              )}
            </div>
            {!compact && user.user_name && (
              <div className="truncate text-[12px] text-gray-500" title={user.user_name}>
                {user.user_name}
              </div>
            )}
          </div>
        </div>
      </td>
      <td className="whitespace-nowrap px-2.5 py-1.5">
        <div
          className={`${compact ? 'inline-flex' : 'flex xl:inline-flex'} items-center gap-1.5 align-middle`}
        >
          <span
            className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${status.pill}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${status.dot}`} aria-hidden="true" />
            {status.label}
          </span>
          {anomalous && (
            <span
              className="inline-flex items-center rounded-full bg-red-100 px-1.5 py-0.5 text-[11px] font-medium text-red-700"
              title="Anomaly: ≥5x 7d avg"
            >
              ⚠
            </span>
          )}
        </div>
        {/* Labelled buttons beside the pill on wide screens, under it when the
            column is narrow; compact rows keep one line with icon buttons. */}
        {user.status === 'pending_approval' && (
          <div
            className={
              compact
                ? 'ml-1 inline-flex gap-1 align-middle'
                : 'mt-1 flex gap-1 xl:ml-1.5 xl:mt-0 xl:inline-flex xl:align-middle'
            }
          >
            <button
              onClick={(e) => {
                e.stopPropagation();
                onApprove();
              }}
              aria-label="Approve"
              title="Approve"
              className="rounded-md bg-emerald-600 px-2 py-0.5 text-[11px] font-semibold text-white transition hover:bg-emerald-700"
            >
              {compact ? '✓' : 'Approve'}
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onReject();
              }}
              aria-label="Reject"
              title="Reject"
              className="rounded-md border border-red-200 bg-white px-2 py-0.5 text-[11px] font-semibold text-red-600 transition hover:bg-red-50"
            >
              {compact ? '✕' : 'Reject'}
            </button>
          </div>
        )}
      </td>
      {visible.has('today') && (
        <td className={`${NUM} ${numTone(today)} ${cellBucket} ${edge('today')}`}>
          ${today.toFixed(2)}
        </td>
      )}
      {visible.has('trend') && (
        <td className={`px-2 ${edge('trend')}`}>
          <Sparkline points={history ?? []} />
        </td>
      )}
      {visible.has('month') && (
        <td className={`${NUM} ${numTone(month)} ${edge('month')}`}>${month.toFixed(2)}</td>
      )}
      {visible.has('alltime') && (
        <td
          className={`${NUM} ${alltime > 0 ? 'text-gray-600' : 'text-gray-300'} ${edge('alltime')}`}
        >
          ${alltime.toFixed(2)}
        </td>
      )}
      {visible.has('requests') && (
        <td
          className={`${NUM} ${numTone(user.usage_alltime_requests)} ${edge('requests')}`}
          title={`${user.usage_alltime_requests.toLocaleString()} requests (all-time)`}
        >
          {user.usage_alltime_requests.toLocaleString()}
        </td>
      )}
      {visible.has('tokens') && (
        <td
          className={`${NUM} ${numTone(user.usage_alltime_tokens)} ${edge('tokens')}`}
          title={`${user.usage_alltime_tokens.toLocaleString()} tokens (all-time)`}
        >
          {formatCompact(user.usage_alltime_tokens)}
        </td>
      )}
      {visible.has('avgTurns') && (
        <td className={`${NUM} text-gray-700 ${edge('avgTurns')}`}>
          {turns?.avg_turns != null ? turns.avg_turns.toFixed(1) : <Dash />}
        </td>
      )}
      {visible.has('avgUserTurns') && (
        <td className={`${NUM} text-gray-700 ${edge('avgUserTurns')}`}>
          {turns?.avg_user_turns != null ? turns.avg_user_turns.toFixed(1) : <Dash />}
        </td>
      )}
      {visible.has('askQuestion') && (
        <td
          className={`${NUM} text-gray-700 ${edge('askQuestion')}`}
          title={
            askQuestion?.ask_question_fraction != null
              ? `${askQuestion.n_ask_requests.toLocaleString()} of ${askQuestion.n_requests.toLocaleString()} opening requests (first 5 turns) offer an ask-question tool`
              : undefined
          }
        >
          {askQuestion?.ask_question_fraction != null ? (
            `${(askQuestion.ask_question_fraction * 100).toFixed(0)}%`
          ) : (
            <Dash />
          )}
        </td>
      )}
      {visible.has('automation') && (
        <td className={`whitespace-nowrap px-2.5 pr-3 text-right text-sm ${edge('automation')}`}>
          {automation ? (
            <span
              className={`inline-flex items-center rounded-full px-1.5 py-0.5 text-[11px] font-medium tabular-nums ring-1 ring-inset ${bandStyle(automation.band).chip}`}
              title={`${bandStyle(automation.band).label} · score ${automation.score.toFixed(2)} · confidence ${automation.confidence.toFixed(2)}${automation.insufficient_data ? ' · insufficient data' : ''}`}
            >
              {automation.score.toFixed(2)}
            </span>
          ) : scoreState === 'loading' ? (
            <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          ) : scoreState === 'loaded' ? (
            <Dash />
          ) : null}
        </td>
      )}
    </tr>
  );
}

function Dash() {
  return <span className="text-gray-300">—</span>;
}
