'use client';

import { ThresholdInput } from '@/components/features/console-shared/ThresholdInput';
import { useMoney } from '@/lib/hooks/useMoney';

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

// No debounce here: FilterBar wraps onChange in applyFilterState which
// already debounces search. Numeric inputs are infrequent enough that
// immediate propagation is fine and avoids stale-closure clobber issues.
export function UsageFilter({ minCostToday, minCostMonth, activeWithinHours, onChange }: Props) {
  const money = useMoney();
  return (
    <div className="flex flex-wrap items-center gap-2 text-[13px]">
      <ThresholdInput
        label={`Today ≥ ${money.symbol}`}
        value={minCostToday}
        onChange={(next) => onChange({ minCostToday: next })}
        step="0.01"
      />
      <ThresholdInput
        label={`Month ≥ ${money.symbol}`}
        value={minCostMonth}
        onChange={(next) => onChange({ minCostMonth: next })}
        step="0.01"
      />
      <ThresholdInput
        label="Active in last (h)"
        value={activeWithinHours}
        onChange={(next) => onChange({ activeWithinHours: next })}
        min={1}
      />
    </div>
  );
}
