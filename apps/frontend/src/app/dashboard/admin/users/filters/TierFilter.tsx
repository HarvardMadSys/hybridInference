'use client';

import type { UserRoleCounts } from '@/lib/api/admin';

import type { UserRole } from '../types';

/**
 * Service accounts are not a tier (each holds one), but they are listed as one
 * chip among the tiers, next to Pro.
 */
export const SERVICE_ACCOUNT_TIER = 'service_account';

export type TierValue = UserRole | typeof SERVICE_ACCOUNT_TIER;

const TIERS: ReadonlyArray<{ role: TierValue; label: string }> = [
  { role: 'free', label: 'Free' },
  { role: 'pro', label: 'Pro' },
  { role: SERVICE_ACCOUNT_TIER, label: 'Service' },
  { role: 'internal', label: 'Internal' },
  { role: 'admin', label: 'Admin' },
];

interface TierFilterProps {
  value: TierValue | null;
  /** Unfiltered account counts. Absent while the list is still loading. */
  counts?: UserRoleCounts;
  /** Unfiltered service-account count. Absent while loading or from an older gateway. */
  serviceAccountCount?: number;
  onChange: (tier: TierValue | null) => void;
}

export function TierFilter({ value, counts, serviceAccountCount, onChange }: TierFilterProps) {
  return (
    <div role="group" aria-label="Account tiers" className="flex flex-wrap items-center gap-1.5">
      <span className="mr-1 text-[12px] font-medium text-gray-500">Tier</span>
      <TierButton label="All" pressed={value === null} onClick={() => onChange(null)} />
      {TIERS.map(({ role, label }) => (
        <TierButton
          key={role}
          label={label}
          count={role === SERVICE_ACCOUNT_TIER ? serviceAccountCount : counts?.[role]}
          pressed={value === role}
          onClick={() => onChange(value === role ? null : role)}
        />
      ))}
    </div>
  );
}

function TierButton({
  label,
  count,
  pressed,
  onClick,
}: {
  label: string;
  count?: number;
  pressed: boolean;
  onClick: () => void;
}) {
  const name = count === undefined ? label : `${label} ${count}`;
  return (
    <button
      type="button"
      aria-label={name}
      aria-pressed={pressed}
      onClick={onClick}
      className={`inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-[12px] font-medium transition ${
        pressed
          ? 'border-gray-900 bg-gray-900 text-white'
          : 'border-gray-200 bg-white text-gray-700 hover:border-gray-300 hover:text-gray-900'
      }`}
    >
      {label}
      {count !== undefined && (
        <span className={`tabular-nums ${pressed ? 'text-white/80' : 'text-gray-500'}`}>
          {count}
        </span>
      )}
    </button>
  );
}
