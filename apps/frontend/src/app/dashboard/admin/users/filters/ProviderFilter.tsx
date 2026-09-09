'use client';

import { useEffect, useState } from 'react';

import { getRoutableProviders, type RoutableProvider } from '@/lib/api/admin';

interface Props {
  value: string | null;
  onChange: (v: string | null) => void;
}

// The choices are whatever this gateway routes to, read from the routing
// table, so the filter never advertises a vendor the deployment does not use.
export function ProviderFilter({ value, onChange }: Props) {
  const [providers, setProviders] = useState<RoutableProvider[]>([]);

  useEffect(() => {
    let cancelled = false;
    getRoutableProviders()
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

  const options = providers.map((p) => ({
    value: p.provider,
    label: p.display_name || p.provider,
  }));
  if (value && !options.some((option) => option.value === value)) {
    options.push({ value, label: value });
  }

  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value || null)}
      className="rounded border bg-white px-2 py-1 text-sm"
    >
      <option value="">Any provider</option>
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}
