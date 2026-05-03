'use client';

import { useEffect, useRef, useState } from 'react';
import type { Density, FilterState } from './types';
import { StatusFilter } from './filters/StatusFilter';
import { UsageFilter } from './filters/UsageFilter';
import { ProviderFilter } from './filters/ProviderFilter';
import { QuotaFilter } from './filters/QuotaFilter';
import { DEFAULT_FILTER_STATE } from './lib/filterTypes';

interface FilterBarProps {
  state: FilterState;
  onChange: (state: FilterState) => void;
  density: Density;
  onDensityChange: (d: Density) => void;
}

export function FilterBar({ state, onChange, density, onDensityChange }: FilterBarProps) {
  const [searchInput, setSearchInput] = useState(state.search);
  // Keep a ref to the latest state so the debounce callback always merges into
  // the most recent state rather than the stale closure value.
  const stateRef = useRef(state);
  stateRef.current = state;

  // Sync searchInput when state.search changes externally (e.g. saved view applied)
  useEffect(() => {
    setSearchInput(state.search);
  }, [state.search]);

  // Debounce search 300ms. Read stateRef.current inside the timeout so we
  // always merge into the latest filter state — not the stale closure copy.
  useEffect(() => {
    const t = setTimeout(() => {
      if (searchInput !== stateRef.current.search) {
        onChange({ ...stateRef.current, search: searchInput });
      }
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput]);

  const isDefault = JSON.stringify(state) === JSON.stringify(DEFAULT_FILTER_STATE);

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-gray-200 bg-gray-50 p-3">
      <input
        type="search"
        placeholder="Search email, key prefix, id…"
        value={searchInput}
        onChange={(e) => setSearchInput(e.target.value)}
        className="min-w-48 flex-1 rounded border border-gray-300 bg-white px-3 py-1 text-sm"
      />
      <StatusFilter value={state.status} onChange={(status) => onChange({ ...stateRef.current, status })} />
      <UsageFilter
        minCostToday={state.minCostToday}
        minCostMonth={state.minCostMonth}
        activeWithinHours={state.activeWithinHours}
        onChange={(patch) => onChange({ ...stateRef.current, ...patch })}
      />
      <ProviderFilter
        value={state.provider}
        onChange={(provider) => onChange({ ...stateRef.current, provider })}
      />
      <QuotaFilter
        value={state.quotaState}
        onChange={(quotaState) => onChange({ ...stateRef.current, quotaState })}
      />
      {!isDefault && (
        <button
          type="button"
          onClick={() => onChange(DEFAULT_FILTER_STATE)}
          className="text-xs text-gray-600 underline hover:text-gray-900"
        >
          Clear filters
        </button>
      )}
      <div className="ml-auto flex items-center gap-1 text-xs text-gray-600">
        <span>Density:</span>
        <button
          type="button"
          onClick={() => onDensityChange('comfortable')}
          className={`rounded px-2 py-0.5 ${density === 'comfortable' ? 'bg-gray-200' : ''}`}
        >
          Comfortable
        </button>
        <button
          type="button"
          onClick={() => onDensityChange('compact')}
          className={`rounded px-2 py-0.5 ${density === 'compact' ? 'bg-gray-200' : ''}`}
        >
          Compact
        </button>
      </div>
    </div>
  );
}
