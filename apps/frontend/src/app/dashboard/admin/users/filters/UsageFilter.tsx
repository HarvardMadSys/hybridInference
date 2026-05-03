'use client';

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
    <div className="flex items-center gap-2 text-sm">
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">today ≥ $</span>
        <input
          type="number"
          min={0}
          step="0.01"
          value={todayInput}
          onChange={(e) => handleTodayChange(e.target.value)}
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">month ≥ $</span>
        <input
          type="number"
          min={0}
          step="0.01"
          value={monthInput}
          onChange={(e) => handleMonthChange(e.target.value)}
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">active in last (h)</span>
        <input
          type="number"
          min={1}
          value={hoursInput}
          onChange={(e) => handleHoursChange(e.target.value)}
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
    </div>
  );
}
