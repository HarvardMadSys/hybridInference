'use client';

import { PANEL } from '@/components/features/admin/adminStyles';
import { METER_FILL } from './parts';

/** One member of a card: who, the figure that put it there, and that figure as a bar. */
export interface MemberCardRow {
  id: string;
  name: string;
  /** The figure, already in words: "89%", "$1.25". */
  value: string;
  /** How much of the bar is filled, from 0 to 1. */
  share: number;
  /** The color of the bar; the colors of a limit in the list below. */
  tone?: keyof typeof METER_FILL;
}

/**
 * A card above the member list: a short list of members under one heading,
 * each with a bar, and how many members the heading holds in all.
 *
 * The card is a switch for the list below. A press opens the list on the
 * card's view; a press on the card that is on puts the list back. The card
 * says which of the two a press does. That line is for the eye: a screen
 * reader has the state of the switch, and the name of the card stays one name.
 */
export function MemberCard({
  title,
  count,
  rows,
  empty,
  loading = false,
  active,
  showLabel,
  clearLabel,
  onToggle,
}: {
  title: string;
  /** How many members the heading holds, in words: "2 members". Left out for none. */
  count?: string;
  rows: MemberCardRow[];
  /** What the card says when it has no member. */
  empty: string;
  loading?: boolean;
  /** The list below is on this card's view. */
  active: boolean;
  /** What a press does when the card is off, and when it is on. */
  showLabel: string;
  clearLabel: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onToggle}
      className={`${PANEL} group flex flex-col p-4 text-left transition hover:border-gray-300 ${
        active ? 'border-transparent ring-2 ring-blue-500' : ''
      }`}
    >
      <span className="flex w-full flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <span className="text-[13px] font-semibold text-gray-900">{title}</span>
        {count ? <span className="text-[12px] tabular-nums text-gray-500">{count}</span> : null}
      </span>
      {rows.length > 0 ? (
        // One grid for the card, and each row on its columns: the figures then
        // have one width, so every bar has the same track.
        <span className="mt-3 grid w-full grid-cols-[minmax(0,9rem)_minmax(0,1fr)_auto] gap-x-3 gap-y-2 text-[13px]">
          {rows.map((row) => (
            <span key={row.id} className="col-span-3 grid grid-cols-subgrid items-center">
              <span className="truncate text-gray-700" title={row.name}>
                {row.name}
              </span>
              <span
                aria-hidden="true"
                className="block h-1.5 overflow-hidden rounded-full bg-gray-300"
              >
                <span
                  className={`block h-full rounded-full ${METER_FILL[row.tone ?? 'ok']}`}
                  // As the bar of a limit: nothing draws no fill, anything draws enough to be seen.
                  style={{
                    width: `${row.share > 0 ? Math.round(Math.max(0.03, Math.min(1, row.share)) * 100) : 0}%`,
                  }}
                />
              </span>
              <span className="whitespace-nowrap text-right tabular-nums text-gray-900">
                {row.value}
              </span>
            </span>
          ))}
        </span>
      ) : (
        <span className="mt-3 text-[12px] text-gray-400">{loading ? '—' : empty}</span>
      )}
      {/* At the foot, so that the cards of a row end alike whatever they list. */}
      <span
        aria-hidden="true"
        className={`mt-auto pt-3 text-[12px] font-medium ${
          active ? 'text-blue-600' : 'text-gray-500 group-hover:text-gray-900'
        }`}
      >
        {active ? clearLabel : showLabel} {active ? '✕' : '→'}
      </span>
    </button>
  );
}
