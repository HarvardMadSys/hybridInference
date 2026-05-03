'use client';

import type { QuotaStateFilter } from '../types';

const OPTIONS: Array<{ value: QuotaStateFilter | ''; label: string }> = [
  { value: '', label: 'Any quota' },
  { value: 'default', label: 'Default quota' },
  { value: 'custom', label: 'Custom quota' },
  { value: 'near', label: 'Near quota (≥80%)' },
  { value: 'over', label: 'Over quota' },
];

interface Props {
  value: QuotaStateFilter | null;
  onChange: (v: QuotaStateFilter | null) => void;
}

export function QuotaFilter({ value, onChange }: Props) {
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange((e.target.value as QuotaStateFilter) || null)}
      className="rounded border bg-white px-2 py-1 text-sm"
    >
      {OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
