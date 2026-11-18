import type { ApiKeyListItem } from '@/lib/api/user';
import type { Translate } from '@/components/providers/useT';
import { formatMoney, moneyAmount, moneyCurrency } from '@/lib/utils/money';
import { fill } from '@/lib/utils/interpolate';

/** Where a key's own page is. */
export function keyDetailHref(keyPrefix: string): string {
  return `/dashboard/keys/${encodeURIComponent(keyPrefix)}`;
}

/**
 * A key's budget in the ledger currency: the native `budget_daily` /
 * `budget_monthly`, or the `*_usd` value on a USD ledger. A USD value is never
 * shown as another currency.
 */
export function keyBudget(
  key: ApiKeyListItem,
  window: 'budget_daily' | 'budget_monthly',
): number | null {
  return moneyAmount(key, window);
}

/** Whether the key has a budget in *window*, whichever field carries it. */
function hasBudget(key: ApiKeyListItem, window: 'budget_daily' | 'budget_monthly'): boolean {
  return key[`${window}_usd`] != null || keyBudget(key, window) != null;
}

/**
 * One line naming a key's own limits, e.g. "$5.00/day · 2 models", or "No limits".
 * Amounts are formatted with *format* (the console's `useMoney().value`), else
 * in the key's own currency.
 */
export function limitsSummary(
  key: ApiKeyListItem,
  t: Translate,
  format: (source: ApiKeyListItem, field: string) => string = (source, field) =>
    formatMoney(moneyAmount(source, field), moneyCurrency(source)),
): string {
  const parts: string[] = [];
  if (hasBudget(key, 'budget_daily')) {
    parts.push(
      fill(t('dashboard.keys.limit_daily', '{amount}/day'), {
        amount: format(key, 'budget_daily'),
      }),
    );
  }
  if (hasBudget(key, 'budget_monthly')) {
    parts.push(
      fill(t('dashboard.keys.limit_monthly', '{amount}/month'), {
        amount: format(key, 'budget_monthly'),
      }),
    );
  }
  if (key.allowed_models != null) {
    parts.push(
      key.allowed_models.length === 1
        ? t('dashboard.keys.limit_models_one', '1 model')
        : fill(t('dashboard.keys.limit_models_other', '{count} models'), {
            count: String(key.allowed_models.length),
          }),
    );
  }
  return parts.length ? parts.join(' · ') : t('dashboard.keys.no_limits', 'No limits');
}

/** Highest budget the server accepts, per day or per month. */
const BUDGET_MAX_USD = 1_000_000;

/**
 * A typed budget as the API takes it: blank is no limit (`null`); otherwise a
 * number from 0 to *max* (by default {@link BUDGET_MAX_USD}) with at most four
 * decimal places. `undefined` when the text is not one. A budget in another
 * currency passes `Infinity`: the server bounds its USD equivalent.
 */
export function parseBudget(text: string, max: number = BUDGET_MAX_USD): number | null | undefined {
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (!/^\d+(\.\d{1,4})?$/.test(trimmed)) return undefined;
  const value = Number(trimmed);
  return value <= max ? value : undefined;
}
