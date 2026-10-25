'use client';

import { useEffect, useState } from 'react';

import { Select } from '@/components/ui/Select';
import { getUserFilterProviders, type UserFilterProvider } from '@/lib/api/admin';

interface Props {
  value: string | null;
  onChange: (v: string | null) => void;
}

function optionLabel(provider: UserFilterProvider): string {
  const name = provider.display_name || provider.provider;
  return provider.routable ? name : `${name} (no longer routed)`;
}

// The filter matches api_logs over the last 30 days, so the choices are what
// that window can match — a provider removed from the routing table stays
// selectable while its traffic is in the log — plus whatever is routed today.
export function ProviderFilter({ value, onChange }: Props) {
  const [providers, setProviders] = useState<UserFilterProvider[]>([]);

  useEffect(() => {
    let cancelled = false;
    getUserFilterProviders()
      .then((resp) => {
        if (!cancelled) setProviders(resp.providers);
      })
      .catch(() => {
        // Leave the list empty; the current selection stays selectable below.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const options = [
    { value: '', label: 'Any provider' },
    ...providers.map((p) => ({ value: p.provider, label: optionLabel(p) })),
  ];
  if (value && !options.some((option) => option.value === value)) {
    options.push({ value, label: value });
  }

  return (
    <Select
      aria-label="Filter by provider"
      value={value ?? ''}
      onValueChange={(v) => onChange(v || null)}
      options={options}
    />
  );
}
