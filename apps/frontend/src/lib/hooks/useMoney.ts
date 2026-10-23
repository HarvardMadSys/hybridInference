'use client';

import { useMemo } from 'react';
import { useLocale } from '@/components/providers/LocaleProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { formatMoney, moneyAmount, moneyCurrency, type Currency } from '@/lib/utils/money';

export function useMoney() {
  const currency = useSiteConfig().accounting?.currency ?? 'USD';
  const locale = useLocale() === 'zh-CN' ? 'zh-CN' : 'en-US';
  return useMemo(
    () => ({
      currency,
      symbol: currency === 'CNY' ? '¥' : '$',
      amount: moneyAmount,
      sourceCurrency: moneyCurrency,
      format: (
        value: number | null | undefined,
        options?: Intl.NumberFormatOptions,
        unit: Currency = currency,
      ) => formatMoney(value, unit, locale, options),
      value: (
        source: object | null | undefined,
        field: string,
        options?: Intl.NumberFormatOptions,
      ) => formatMoney(moneyAmount(source, field), moneyCurrency(source), locale, options),
      tokenCost: (source: object | null | undefined) => {
        const value = moneyAmount(source, 'cost');
        const digits = value === 0 ? 0 : value != null && Math.abs(value) < 1 ? 4 : 2;
        return formatMoney(value, moneyCurrency(source), locale, {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        });
      },
      requestCost: (source: object | null | undefined, tinyThreshold = false) => {
        const value = moneyAmount(source, 'cost');
        if (value == null) return '—';
        const unit = moneyCurrency(source);
        if (tinyThreshold && value > 0 && value < 0.0001) {
          return `<${formatMoney(0.0001, unit, locale, { minimumFractionDigits: 4 })}`;
        }
        const digits = value > 0 && value < 0.01 ? 4 : 2;
        return formatMoney(value, unit, locale, {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        });
      },
    }),
    [currency, locale],
  );
}
