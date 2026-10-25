'use client';

import { Select } from '@/components/ui/Select';

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
    <Select
      aria-label="Filter by quota"
      value={value ?? ''}
      onValueChange={(v) => onChange((v as QuotaStateFilter) || null)}
      options={OPTIONS}
    />
  );
}
