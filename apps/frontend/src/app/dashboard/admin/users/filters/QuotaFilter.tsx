'use client';

import type { QuotaStateFilter } from '../types';
import { CONTROL } from '../ui';

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
      className={CONTROL}
    >
      {OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
