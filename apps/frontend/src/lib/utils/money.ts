export type Currency = 'USD' | 'CNY';

export function moneyCurrency(source: object | null | undefined): Currency {
  return (source as { currency?: Currency } | null)?.currency === 'CNY' ? 'CNY' : 'USD';
}

/** Native amounts take precedence. A USD alias is never a CNY amount. */
export function moneyAmount(source: object | null | undefined, field: string): number | null {
  if (!source) return null;
  const record = source as Record<string, unknown>;
  const raw =
    record[field] !== undefined
      ? record[field]
      : moneyCurrency(source) === 'USD'
        ? record[`${field}_usd`]
        : undefined;
  if (raw === null || raw === undefined || raw === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

export function formatMoney(
  value: number | null | undefined,
  currency: Currency,
  locale = 'en-US',
  options: Intl.NumberFormatOptions = {},
): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency,
    currencyDisplay: 'narrowSymbol',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
    ...options,
  }).format(value);
}
