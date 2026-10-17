'use client';

import type { UserStatus } from '../types';
import { CONTROL } from '../ui';

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
    <select
      value={value ?? ''}
      onChange={(e) => onChange((e.target.value as UserStatus) || null)}
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
