'use client';

import { useMoney } from '@/lib/hooks/useMoney';
import { useEffect, useState } from 'react';

interface Props {
  minCostToday: number | null;
  minCostMonth: number | null;
  activeWithinHours: number | null;
  onChange: (patch: {
    minCostToday?: number | null;
    minCostMonth?: number | null;
    activeWithinHours?: number | null;
  }) => void;
}

function toInputString(v: number | null): string {
  return v === null ? '' : String(v);
}

// Return null for empty/non-finite strings to avoid NaN in state/URL params.
function parseInput(s: string): number | null {
  if (s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

export function UsageFilter({ minCostToday, minCostMonth, activeWithinHours, onChange }: Props) {
  const money = useMoney();
  const [todayInput, setTodayInput] = useState(toInputString(minCostToday));
  const [monthInput, setMonthInput] = useState(toInputString(minCostMonth));
  const [hoursInput, setHoursInput] = useState(toInputString(activeWithinHours));

  // Sync local inputs when parent state changes externally (e.g. saved view applied)
  useEffect(() => {
    setTodayInput(toInputString(minCostToday));
  }, [minCostToday]);
  useEffect(() => {
    setMonthInput(toInputString(minCostMonth));
  }, [minCostMonth]);
  useEffect(() => {
    setHoursInput(toInputString(activeWithinHours));
  }, [activeWithinHours]);

  // No debounce here: FilterBar wraps onChange in applyFilterState which
  // already debounces search. Numeric inputs are infrequent enough that
  // immediate propagation is fine and avoids stale-closure clobber issues.
  const handleTodayChange = (val: string) => {
    setTodayInput(val);
    const next = parseInput(val);
    if (next !== minCostToday) onChange({ minCostToday: next });
  };

  const handleMonthChange = (val: string) => {
    setMonthInput(val);
    const next = parseInput(val);
    if (next !== minCostMonth) onChange({ minCostMonth: next });
  };

  const handleHoursChange = (val: string) => {
    setHoursInput(val);
    const next = parseInput(val);
    if (next !== activeWithinHours) onChange({ activeWithinHours: next });
  };

  return (
    <div className="flex flex-wrap items-center gap-2 text-[13px]">
      <label className="flex h-8 items-center gap-1.5 rounded-md border border-gray-200 bg-white pl-2.5 pr-1 focus-within:border-gray-400">
        <span className="whitespace-nowrap text-[12px] text-gray-500">Today ≥ {money.symbol}</span>
        <input
          type="number"
          min={0}
          step="0.01"
          value={todayInput}
          onChange={(e) => handleTodayChange(e.target.value)}
          className="w-16 bg-transparent px-1 text-[13px] tabular-nums text-gray-900 placeholder:text-gray-300 focus:outline-none"
          placeholder="any"
        />
      </label>
      <label className="flex h-8 items-center gap-1.5 rounded-md border border-gray-200 bg-white pl-2.5 pr-1 focus-within:border-gray-400">
        <span className="whitespace-nowrap text-[12px] text-gray-500">Month ≥ {money.symbol}</span>
        <input
          type="number"
          min={0}
          step="0.01"
          value={monthInput}
          onChange={(e) => handleMonthChange(e.target.value)}
          className="w-16 bg-transparent px-1 text-[13px] tabular-nums text-gray-900 placeholder:text-gray-300 focus:outline-none"
          placeholder="any"
        />
      </label>
      <label className="flex h-8 items-center gap-1.5 rounded-md border border-gray-200 bg-white pl-2.5 pr-1 focus-within:border-gray-400">
        <span className="whitespace-nowrap text-[12px] text-gray-500">Active in last (h)</span>
        <input
          type="number"
          min={1}
          value={hoursInput}
          onChange={(e) => handleHoursChange(e.target.value)}
          className="w-16 bg-transparent px-1 text-[13px] tabular-nums text-gray-900 placeholder:text-gray-300 focus:outline-none"
          placeholder="any"
        />
      </label>
    </div>
  );
}
