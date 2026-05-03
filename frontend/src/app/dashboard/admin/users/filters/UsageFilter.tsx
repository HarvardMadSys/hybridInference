'use client';

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

export function UsageFilter({ minCostToday, minCostMonth, activeWithinHours, onChange }: Props) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">today ≥ $</span>
        <input
          type="number"
          min={0}
          step="0.01"
          value={minCostToday ?? ''}
          onChange={(e) =>
            onChange({ minCostToday: e.target.value === '' ? null : Number(e.target.value) })
          }
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">month ≥ $</span>
        <input
          type="number"
          min={0}
          step="0.01"
          value={minCostMonth ?? ''}
          onChange={(e) =>
            onChange({ minCostMonth: e.target.value === '' ? null : Number(e.target.value) })
          }
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">active in last (h)</span>
        <input
          type="number"
          min={1}
          value={activeWithinHours ?? ''}
          onChange={(e) =>
            onChange({ activeWithinHours: e.target.value === '' ? null : Number(e.target.value) })
          }
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
    </div>
  );
}
