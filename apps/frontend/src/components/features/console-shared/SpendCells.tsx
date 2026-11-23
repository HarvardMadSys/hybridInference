'use client';

import { Sparkline } from './Sparkline';

/** The spend columns of an account list, in the order the list shows them. */
export type SpendColumn = 'today' | 'trend' | 'month' | 'alltime';

/** One amount of a row: the number, to dim a zero by, and its text in the ledger currency. */
export interface SpendFigure {
  amount: number;
  text: string;
}

// Numeric cells: right-aligned tabular figures; a zero is dimmed so the eye
// lands on the accounts that actually have activity.
export const SPEND_CELL = 'whitespace-nowrap px-2.5 text-right text-[13px] tabular-nums';
const tone = (amount: number) => (amount > 0 ? 'text-gray-900' : 'text-gray-300');

/**
 * The spend cells of one row of an account list: today, the trend of the last
 * days, this month and all time. The caller reads the amounts and writes them
 * in the ledger currency; a list can leave a column out and add its own
 * classes to a cell (a group edge, a mark on a high figure).
 */
export function SpendCells({
  today,
  trend,
  trendLabel,
  month,
  alltime,
  shows = () => true,
  cellClass = () => '',
  numberClass = SPEND_CELL,
}: {
  today: SpendFigure;
  /** One value for each of the last days, the oldest first. */
  trend: Array<number | null>;
  trendLabel: string;
  month: SpendFigure;
  alltime: SpendFigure;
  shows?: (column: SpendColumn) => boolean;
  cellClass?: (column: SpendColumn) => string;
  /** The classes of a figure's cell, for a table with other cell sizes. */
  numberClass?: string;
}) {
  return (
    <>
      {shows('today') && (
        <td className={`${numberClass} ${tone(today.amount)} ${cellClass('today')}`}>
          {today.text}
        </td>
      )}
      {shows('trend') && (
        <td className={`px-2 ${cellClass('trend')}`}>
          <Sparkline values={trend} label={trendLabel} />
        </td>
      )}
      {shows('month') && (
        <td className={`${numberClass} ${tone(month.amount)} ${cellClass('month')}`}>
          {month.text}
        </td>
      )}
      {shows('alltime') && (
        <td
          className={`${numberClass} ${alltime.amount > 0 ? 'text-gray-600' : 'text-gray-300'} ${cellClass('alltime')}`}
        >
          {alltime.text}
        </td>
      )}
    </>
  );
}
