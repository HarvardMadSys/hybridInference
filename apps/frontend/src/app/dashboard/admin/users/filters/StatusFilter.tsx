'use client';

import { Select } from '@/components/ui/Select';

import type { UserStatus } from '../types';

const OPTIONS: Array<{ value: UserStatus | ''; label: string }> = [
  { value: '', label: 'All statuses' },
  { value: 'pending_approval', label: 'Pending' },
  { value: 'active', label: 'Active' },
  { value: 'suspended', label: 'Suspended' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'deleted', label: 'Deleted' },
];

interface Props {
  value: UserStatus | null;
  onChange: (value: UserStatus | null) => void;
}

export function StatusFilter({ value, onChange }: Props) {
  return (
    <Select
      aria-label="Filter by status"
      value={value ?? ''}
      onValueChange={(v) => onChange((v as UserStatus) || null)}
      options={OPTIONS}
    />
  );
}
