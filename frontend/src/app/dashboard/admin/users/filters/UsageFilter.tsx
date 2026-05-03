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

function parseInput(s: string): number | null {
  return s === '' ? null : Number(s);
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

  // Debounce 300ms (matches FilterBar search debounce)
  useEffect(() => {
    const t = setTimeout(() => {
      const next = parseInput(todayInput);
      if (next !== minCostToday) onChange({ minCostToday: next });
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [todayInput]);

  useEffect(() => {
    const t = setTimeout(() => {
      const next = parseInput(monthInput);
      if (next !== minCostMonth) onChange({ minCostMonth: next });
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [monthInput]);

  useEffect(() => {
    const t = setTimeout(() => {
      const next = parseInput(hoursInput);
      if (next !== activeWithinHours) onChange({ activeWithinHours: next });
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hoursInput]);

  return (
    <div className="flex items-center gap-2 text-sm">
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">today ≥ $</span>
        <input
          type="number"
          min={0}
          step="0.01"
          value={todayInput}
          onChange={(e) => setTodayInput(e.target.value)}
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
          onChange={(e) => setMonthInput(e.target.value)}
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">active in last (h)</span>
        <input
          type="number"
          min={1}
          value={hoursInput}
          onChange={(e) => setHoursInput(e.target.value)}
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
    </div>
  );
}
