const MIN_HISTORY_DAYS = 3;
const MIN_TODAY_FLOOR_USD = 1.0;
const MULTIPLIER = 5.0;

export interface AnomalyOptions {
  returnMultiplier?: boolean;
}

export function isAnomalous(
  todayCostUsd: number,
  priorDailyCostsUsd: number[],
): boolean;
export function isAnomalous(
  todayCostUsd: number,
  priorDailyCostsUsd: number[],
  options: { returnMultiplier: true },
): number | false;
export function isAnomalous(
  todayCostUsd: number,
  priorDailyCostsUsd: number[],
  options: AnomalyOptions = {},
): boolean | number {
  if (priorDailyCostsUsd.length < MIN_HISTORY_DAYS) return false;
  if (todayCostUsd < MIN_TODAY_FLOOR_USD) return false;

  const sum = priorDailyCostsUsd.reduce((a, b) => a + b, 0);
  const avg = sum / priorDailyCostsUsd.length;
  if (avg <= 0) return false;

  const ratio = todayCostUsd / avg;
  const flagged = ratio >= MULTIPLIER;
  if (!flagged) return false;
  return options.returnMultiplier ? ratio : true;
}
