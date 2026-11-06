import type { ApiKeyListItem } from '@/lib/api/user';
import type { Translate } from '@/components/providers/useT';
import { formatUsd } from '@/lib/utils/currency';
import { fill } from '@/lib/utils/interpolate';

/** Where a key's own page is. */
export function keyDetailHref(keyPrefix: string): string {
  return `/dashboard/keys/${encodeURIComponent(keyPrefix)}`;
}

/** One line naming a key's own limits, e.g. "$5.00/day · 2 models", or "No limits". */
export function limitsSummary(key: ApiKeyListItem, t: Translate): string {
  const parts: string[] = [];
  if (key.budget_daily_usd != null) {
    parts.push(
      fill(t('dashboard.keys.limit_daily', '{amount}/day'), {
        amount: formatUsd(key.budget_daily_usd),
      }),
    );
  }
  if (key.budget_monthly_usd != null) {
    parts.push(
      fill(t('dashboard.keys.limit_monthly', '{amount}/month'), {
        amount: formatUsd(key.budget_monthly_usd),
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
export const BUDGET_MAX_USD = 1_000_000;

/**
 * A typed budget as the API takes it: blank is no limit (`null`); otherwise a
 * number from 0 to {@link BUDGET_MAX_USD} with at most four decimal places.
 * `undefined` when the text is not one.
 */
export function parseBudget(text: string): number | null | undefined {
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (!/^\d+(\.\d{1,4})?$/.test(trimmed)) return undefined;
  const value = Number(trimmed);
  return value <= BUDGET_MAX_USD ? value : undefined;
}
