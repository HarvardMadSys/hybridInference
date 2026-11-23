'use client';

import { SummaryCard, type SummaryAccent } from '@/components/features/console-shared/SummaryCard';
import { useMoney } from '@/lib/hooks/useMoney';
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
  accent: SummaryAccent;
  card: SummaryCardData | undefined;
  loading: boolean;
  showCost?: boolean;
  active?: boolean;
  onClick: () => void;
}

/** One card of the Users tab: the shared card, with the accounts named as the tab names them. */
function Card({ title, accent, card, loading, showCost, active, onClick }: CardProps) {
  const money = useMoney();
  return (
    <SummaryCard
      title={title}
      accent={accent}
      count={card?.count ?? 0}
      items={(card?.top ?? []).map((u) => ({
        id: u.id,
        label: userAccountLabel(u),
        value: showCost ? money.value(u, 'today_cost', { maximumFractionDigits: 2 }) : undefined,
      }))}
      empty="No users right now"
      loading={loading}
      active={active}
      onClick={onClick}
    />
  );
}
