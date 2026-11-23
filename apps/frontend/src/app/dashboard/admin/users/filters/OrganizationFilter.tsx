'use client';

import { Select } from '@/components/ui/Select';
import type { AdminOrganization } from '@/lib/api/admin';

/** The value that keeps the accounts that are members of no organization. */
export const NO_ORGANIZATION = 'none';

interface Props {
  organizations: AdminOrganization[];
  value: string | null;
  onChange: (value: string | null) => void;
}

/** Keeps the members of one organization, or the accounts that have none. */
export function OrganizationFilter({ organizations, value, onChange }: Props) {
  const options = [
    { value: '', label: 'Any organization' },
    { value: NO_ORGANIZATION, label: 'No organization' },
    ...organizations.map((organization) => ({ value: organization.id, label: organization.name })),
  ];
  // A filter from a link that names an organization the list does not have:
  // show it, rather than claim "Any organization" while it applies.
  if (value && !options.some((option) => option.value === value)) {
    options.push({ value, label: value });
  }
  return (
    <Select
      aria-label="Filter by organization"
      value={value ?? ''}
      onValueChange={(next) => onChange(next || null)}
      options={options}
    />
  );
}
