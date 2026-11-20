import { parseBudget } from '@/components/features/dashboard/keyLimits';
import type { Currency } from '@/lib/utils/money';

/** The highest monthly limit the gateway takes in USD. */
const LIMIT_MAX_USD = 999_999.9999;

/**
 * A typed limit: `null` for an empty field, which is no limit; an amount from
 * zero with at most four decimal places; or `undefined` for anything else.
 * Only plain digits are an amount: `1e3` and `0x10` are not.
 */
export function parseLimit(value: string): number | null | undefined {
  return parseBudget(value, Infinity);
}

/** A limit field read for a save: its amount and whether that is a change, or what is wrong with it. */
export type LimitRead =
  | { amount: number | null; changed: boolean }
  | { problem: 'invalid' | 'above' };

/**
 * Reads a limit field against the limit that is stored.
 *
 * A field left as it was read is the stored amount, and is not checked again:
 * a migrated amount may carry more decimal places than a new one may be typed
 * with, and the platform administrator may have lowered the limit of the
 * organization below it. The same amount typed another way, 40.0 for 40, is
 * no change either.
 *
 * `ceiling` is the limit of the organization, which a new amount cannot pass.
 */
export function readLimit(
  value: string,
  stored: number | null,
  currency: Currency,
  ceiling: number | null,
): LimitRead {
  const untouched = value.trim() === (stored === null ? '' : String(stored));
  const amount = untouched ? stored : parseLimit(value);
  if (amount === undefined) return { problem: 'invalid' };
  if (amount === stored) return { amount, changed: false };
  if (amount !== null && ceiling !== null && amount > ceiling) return { problem: 'above' };
  // On another ledger the gateway bounds the USD equivalent of the amount.
  if (amount !== null && currency === 'USD' && amount > LIMIT_MAX_USD)
    return { problem: 'invalid' };
  return { amount, changed: true };
}
