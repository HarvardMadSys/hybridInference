'use client';

import type { ReactNode } from 'react';

const ACCENT = {
  indigo: { dot: 'bg-indigo-500', ring: 'ring-indigo-500', count: 'text-indigo-700' },
  emerald: { dot: 'bg-emerald-500', ring: 'ring-emerald-500', count: 'text-emerald-700' },
  red: { dot: 'bg-red-500', ring: 'ring-red-500', count: 'text-red-700' },
  amber: { dot: 'bg-amber-500', ring: 'ring-amber-500', count: 'text-amber-700' },
} as const;

export type SummaryAccent = keyof typeof ACCENT;

/** One account under a card's count: who, and the figure that put it there. */
export interface SummaryCardItem {
  id: string;
  label: string;
  /** Left out for a card that only names accounts. */
  value?: ReactNode;
}

/**
 * A card above an account list: how many accounts a view holds, and the first
 * few of them. The card is a button that opens the list on that view; the
 * caller reads the figures and names the view.
 */
export function SummaryCard({
  title,
  accent,
  count,
  items,
  empty,
  loading = false,
  active = false,
  onClick,
}: {
  title: string;
  accent: SummaryAccent;
  count: number;
  /** In the order to show; the card shows the first three. */
  items: SummaryCardItem[];
  /** What the card says under a count of zero. */
  empty: string;
  loading?: boolean;
  /** The list is on this card's view. */
  active?: boolean;
  onClick: () => void;
}) {
  const a = ACCENT[accent];
  const top = items.slice(0, 3);
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
          {top.map((item) => (
            <li key={item.id} className="flex justify-between gap-2">
              <span className="truncate">{item.label}</span>
              {item.value !== undefined && (
                <span className="shrink-0 tabular-nums text-gray-900">{item.value}</span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        !loading && <div className="mt-1.5 text-[12px] text-gray-400">{empty}</div>
      )}
    </button>
  );
}
