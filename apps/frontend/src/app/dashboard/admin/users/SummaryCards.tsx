'use client';

import { useUsersSummary } from './hooks/useUsersSummary';
import type { SummaryCard as SummaryCardData } from './types';

export type SummaryCardId = 'pending' | 'top-spenders-today' | 'anomalies' | 'near-quota';

interface SummaryCardsProps {
  onCardClick: (cardId: SummaryCardId) => void;
}

export function SummaryCards({ onCardClick }: SummaryCardsProps) {
  const { data, isLoading, error } = useUsersSummary();

  if (error) {
    return (
      <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">
        Could not load summary stats — table is still usable below.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
      <Card
        title="Pending Approval"
        accent="indigo"
        loading={isLoading}
        card={data?.pending}
        onClick={() => onCardClick('pending')}
      />
      <Card
        title="Top Spenders Today"
        accent="emerald"
        loading={isLoading}
        card={data?.top_spenders_today}
        showCost
        onClick={() => onCardClick('top-spenders-today')}
      />
      <Card
        title="Anomalies"
        accent="red"
        loading={isLoading}
        card={data?.anomalies}
        showCost
        onClick={() => onCardClick('anomalies')}
      />
      <Card
        title="Near / Over Quota"
        accent="amber"
        loading={isLoading}
        card={data?.near_quota}
        showCost
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
  onClick: () => void;
}

function Card({ title, accent, card, loading, showCost, onClick }: CardProps) {
  const accentBorder = {
    indigo: 'border-indigo-200',
    emerald: 'border-emerald-200',
    red: 'border-red-200',
    amber: 'border-amber-200',
  }[accent];
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-lg border ${accentBorder} bg-white p-4 text-left transition-shadow hover:shadow-md`}
    >
      <div className="text-xs uppercase tracking-wide text-gray-500">{title}</div>
      <div className="mt-1 text-2xl font-semibold">{loading ? '—' : (card?.count ?? 0)}</div>
      <ul className="mt-2 space-y-1 text-sm text-gray-700">
        {(card?.top ?? []).slice(0, 3).map((u) => (
          <li key={u.id} className="flex justify-between gap-2 truncate">
            <span className="truncate">{u.email}</span>
            {showCost && (
              <span className="font-mono text-xs">${Number(u.today_cost_usd).toFixed(2)}</span>
            )}
          </li>
        ))}
      </ul>
    </button>
  );
}
