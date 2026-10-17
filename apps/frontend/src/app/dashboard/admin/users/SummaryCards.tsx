'use client';

import { useUsersSummary } from './hooks/useUsersSummary';
import type { SummaryCard as SummaryCardData } from './types';
import { userAccountLabel } from '@/lib/utils/userLabel';

export type SummaryCardId = 'pending' | 'top-spenders-today' | 'anomalies' | 'near-quota';

interface SummaryCardsProps {
  onCardClick: (cardId: SummaryCardId) => void;
  /** The applied view id, so the matching card shows as selected. */
  activeView?: string | null;
}

export function SummaryCards({ onCardClick, activeView }: SummaryCardsProps) {
  const { data, isLoading, error } = useUsersSummary();

  if (error) {
    return (
      <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">
        Could not load summary stats — table is still usable below.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Card
        title="Pending Approval"
        accent="indigo"
        loading={isLoading}
        card={data?.pending}
        active={activeView === 'pending'}
        onClick={() => onCardClick('pending')}
      />
      <Card
        title="Top Spenders Today"
        accent="emerald"
        loading={isLoading}
        card={data?.top_spenders_today}
        showCost
        active={activeView === 'top-spenders-today'}
        onClick={() => onCardClick('top-spenders-today')}
      />
      <Card
        title="Anomalies"
        accent="red"
        loading={isLoading}
        card={data?.anomalies}
        showCost
        active={activeView === 'anomalies'}
        onClick={() => onCardClick('anomalies')}
      />
      <Card
        title="Near / Over Quota"
        accent="amber"
        loading={isLoading}
        card={data?.near_quota}
        showCost
        active={activeView === 'near-quota'}
        onClick={() => onCardClick('near-quota')}
      />
    </div>
  );
}

interface CardProps {
  title: string;
  accent: 'indigo' | 'emerald' | 'red' | 'amber';
  card: SummaryCardData | undefined;
  loading: boolean;
  showCost?: boolean;
  active?: boolean;
  onClick: () => void;
}

const ACCENT = {
  indigo: { dot: 'bg-indigo-500', ring: 'ring-indigo-500', count: 'text-indigo-700' },
  emerald: { dot: 'bg-emerald-500', ring: 'ring-emerald-500', count: 'text-emerald-700' },
  red: { dot: 'bg-red-500', ring: 'ring-red-500', count: 'text-red-700' },
  amber: { dot: 'bg-amber-500', ring: 'ring-amber-500', count: 'text-amber-700' },
} as const;

function Card({ title, accent, card, loading, showCost, active, onClick }: CardProps) {
  const a = ACCENT[accent];
  const count = card?.count ?? 0;
  const top = (card?.top ?? []).slice(0, 3);
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`group flex min-h-[7.5rem] flex-col rounded-lg border bg-white p-4 text-left transition hover:border-gray-300 hover:shadow-sm ${
        active ? `border-transparent ring-2 ${a.ring}` : 'border-gray-200'
      }`}
    >
      <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-wide text-gray-500">
        <span className={`h-2 w-2 rounded-full ${a.dot}`} aria-hidden="true" />
        {title}
      </div>
      <div
        className={`mt-1 text-[26px] font-semibold leading-tight tabular-nums ${
          !loading && count > 0 ? a.count : 'text-gray-300'
        }`}
      >
        {loading ? '—' : count}
      </div>
      {top.length > 0 ? (
        <ul className="mt-1.5 space-y-0.5 text-[12px] text-gray-600">
          {top.map((u) => (
            <li key={u.id} className="flex justify-between gap-2">
              <span className="truncate">{userAccountLabel(u)}</span>
              {showCost && (
                <span className="shrink-0 tabular-nums text-gray-900">
                  ${Number(u.today_cost_usd).toFixed(2)}
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        !loading && <div className="mt-1.5 text-[12px] text-gray-400">No users right now</div>
      )}
    </button>
  );
}
