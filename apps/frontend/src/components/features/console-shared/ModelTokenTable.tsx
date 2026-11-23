'use client';

import type { ReactNode } from 'react';
import { useT } from '@/components/providers/useT';

/** The figure columns of the table, in the order it shows them. */
export type ModelTokenColumn = 'input' | 'output' | 'cached' | 'reasoning' | 'requests' | 'cost';

export const MODEL_TOKEN_COLUMNS: readonly ModelTokenColumn[] = [
  'input',
  'output',
  'cached',
  'reasoning',
  'requests',
  'cost',
];

/** One model's tokens and requests in a period. `cost` is already written in its currency. */
export interface ModelTokenRow {
  model_id: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  reasoning_tokens?: number;
  request_count: number;
  cost?: ReactNode;
}

/**
 * The tokens of each model in a period: input, output, cached and reasoning
 * tokens, requests and cost. The caller reads the rows, formats a count and
 * chooses the columns; `title` heads a table that is one of several.
 */
export function ModelTokenTable({
  title,
  rows,
  formatCount,
  columns = MODEL_TOKEN_COLUMNS,
}: {
  title?: string;
  rows: ModelTokenRow[];
  formatCount: (value: number) => string;
  columns?: readonly ModelTokenColumn[];
}) {
  const t = useT();
  const shown = MODEL_TOKEN_COLUMNS.filter((column) => columns.includes(column));
  const header: Record<ModelTokenColumn, string> = {
    input: t('console.tokens.input', 'Input'),
    output: t('console.tokens.output', 'Output'),
    cached: t('console.tokens.cached', 'Cached'),
    reasoning: t('console.tokens.reasoning', 'Reasoning'),
    requests: t('console.tokens.requests', 'Requests'),
    cost: t('console.tokens.cost', 'Cost'),
  };
  const cell = (row: ModelTokenRow, column: ModelTokenColumn): ReactNode => {
    switch (column) {
      case 'input':
        return formatCount(row.input_tokens);
      case 'output':
        return formatCount(row.output_tokens);
      case 'cached':
        return formatCount(row.cached_tokens);
      case 'reasoning':
        return formatCount(row.reasoning_tokens ?? 0);
      case 'requests':
        return formatCount(row.request_count);
      case 'cost':
        return row.cost;
    }
  };
  // The last column has the padding of the table's edge, as the first one has.
  const pad = (column: ModelTokenColumn) => (column === shown[shown.length - 1] ? 'px-4' : 'px-3');
  return (
    <div className="rounded-xl border overflow-hidden">
      {title !== undefined && (
        <div className="bg-gray-50 px-4 py-2 text-[13px] font-semibold text-gray-900">{title}</div>
      )}
      <table className="w-full text-[12px]">
        <thead className="text-gray-500">
          <tr className="border-t">
            <th className="text-left px-4 py-2">{t('console.tokens.model', 'Model')}</th>
            {shown.map((column) => (
              <th key={column} className={`text-right ${pad(column)} py-2`}>
                {header[column]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.model_id} className="border-t">
              <td className="px-4 py-2 text-gray-900">{row.model_id}</td>
              {shown.map((column) => (
                <td key={column} className={`${pad(column)} py-2 text-right tabular-nums`}>
                  {cell(row, column)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
