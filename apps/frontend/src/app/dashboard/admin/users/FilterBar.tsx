'use client';

import { useEffect, useRef, useState } from 'react';
import type { Density, FilterState } from './types';
import { StatusFilter } from './filters/StatusFilter';
import { UsageFilter } from './filters/UsageFilter';
import { ProviderFilter } from './filters/ProviderFilter';
import { QuotaFilter } from './filters/QuotaFilter';
import { DEFAULT_FILTER_STATE } from './lib/filterTypes';
import { CONTROL } from './ui';
import { ColumnPicker } from './ColumnPicker';
import type { ColumnId } from './lib/columns';

const DENSITIES: Array<[Density, string]> = [
  ['comfortable', 'Comfortable'],
  ['compact', 'Compact'],
];

interface FilterBarProps {
  state: FilterState;
  onChange: (state: FilterState) => void;
  density: Density;
  onDensityChange: (d: Density) => void;
  hiddenColumns: ColumnId[];
  onHiddenColumnsChange: (hidden: ColumnId[]) => void;
}

export function FilterBar({
  state,
  onChange,
  density,
  onDensityChange,
  hiddenColumns,
  onHiddenColumnsChange,
}: FilterBarProps) {
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
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[14rem] flex-1">
          <svg
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.452 4.391l3.328 3.329a.75.75 0 11-1.06 1.06l-3.329-3.328A7 7 0 012 9z"
              clipRule="evenodd"
            />
          </svg>
          <input
            type="search"
            placeholder="Search email, key prefix, id…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            className={`${CONTROL} w-full pl-8`}
          />
        </div>
        <StatusFilter
          value={state.status}
          onChange={(status) => onChange({ ...stateRef.current, status })}
        />
        <ProviderFilter
          value={state.provider}
          onChange={(provider) => onChange({ ...stateRef.current, provider })}
        />
        <QuotaFilter
          value={state.quotaState}
          onChange={(quotaState) => onChange({ ...stateRef.current, quotaState })}
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <UsageFilter
          minCostToday={state.minCostToday}
          minCostMonth={state.minCostMonth}
          activeWithinHours={state.activeWithinHours}
          onChange={(patch) => onChange({ ...stateRef.current, ...patch })}
        />
        {!isDefault && (
          <button
            type="button"
            onClick={() => onChange(DEFAULT_FILTER_STATE)}
            className="h-8 rounded-md px-2 text-[12px] font-medium text-gray-500 transition hover:bg-gray-100 hover:text-gray-900"
          >
            Clear filters
          </button>
        )}
        <div className="ml-auto flex items-center gap-2 text-[12px] text-gray-500">
          <ColumnPicker hidden={hiddenColumns} onChange={onHiddenColumnsChange} />
          <span className="ml-1">Density</span>
          <div className="inline-flex rounded-md border border-gray-200 bg-gray-50 p-0.5">
            {DENSITIES.map(([d, label]) => (
              <button
                key={d}
                type="button"
                onClick={() => onDensityChange(d)}
                aria-pressed={density === d}
                className={`rounded px-2.5 py-1 font-medium transition ${
                  density === d
                    ? 'bg-white text-gray-900 shadow-sm'
                    : 'text-gray-500 hover:text-gray-900'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
