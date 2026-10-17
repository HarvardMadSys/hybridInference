// Optional metric columns of the admin Users table. User and Status are always
// shown; everything here can be hidden from the "Columns" menu, and the choice
// is remembered per browser.

export type ColumnId =
  | 'today'
  | 'trend'
  | 'month'
  | 'alltime'
  | 'requests'
  | 'tokens'
  | 'avgTurns'
  | 'avgUserTurns'
  | 'askQuestion'
  | 'automation';

export interface ColumnGroup {
  label: string;
  columns: Array<{ id: ColumnId; label: string }>;
}

export const COLUMN_GROUPS: ColumnGroup[] = [
  {
    label: 'Spend (USD)',
    columns: [
      { id: 'today', label: 'Today' },
      { id: 'trend', label: '7-day trend' },
      { id: 'month', label: 'Month' },
      { id: 'alltime', label: 'All-time' },
    ],
  },
  {
    label: 'Usage · all-time',
    columns: [
      { id: 'requests', label: 'Requests' },
      { id: 'tokens', label: 'Tokens' },
    ],
  },
  {
    label: 'Behavior',
    columns: [
      { id: 'avgTurns', label: 'Avg turns' },
      { id: 'avgUserTurns', label: 'Avg user turns' },
      { id: 'askQuestion', label: 'Ask-question' },
      { id: 'automation', label: 'Automation score' },
    ],
  },
];

const ALL_IDS = new Set<string>(COLUMN_GROUPS.flatMap((g) => g.columns.map((c) => c.id)));

/** Parse a stored hidden-column list, dropping ids this version doesn't know. */
export function parseHiddenColumns(raw: string | null): ColumnId[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((v): v is ColumnId => typeof v === 'string' && ALL_IDS.has(v));
  } catch {
    return [];
  }
}
