import { describe, expect, it } from 'vitest';
import { formatMoney, moneyAmount, moneyCurrency } from './money';

describe('native accounting amounts', () => {
  it('uses the native amount without applying an exchange rate', () => {
    const row = { currency: 'CNY', cost: '2.00', cost_usd: 0.3 };
    expect(moneyAmount(row, 'cost')).toBe(2);
    expect(formatMoney(moneyAmount(row, 'cost'), moneyCurrency(row))).toBe('¥2.00');
  });

  it('never relabels a legacy USD value as CNY when the native field is absent', () => {
    expect(moneyAmount({ currency: 'CNY', cost_usd: 9 }, 'cost')).toBeNull();
    expect(moneyAmount({ currency: 'CNY', cost: null, cost_usd: 9 }, 'cost')).toBeNull();
  });

  it('supports older USD responses and preserves zero separately from unknown', () => {
    expect(moneyAmount({ cost_usd: 0 }, 'cost')).toBe(0);
    expect(moneyAmount({ currency: 'USD', cost_usd: 0.003 }, 'cost')).toBe(0.003);
    expect(moneyAmount({ cost_usd: null }, 'cost')).toBeNull();
    expect(formatMoney(null, 'CNY')).toBe('—');
  });

  it('keeps request precision in either interface language', () => {
    expect(formatMoney(0.003, 'CNY', 'zh-CN')).toBe('¥0.003');
    expect(formatMoney(0.003, 'USD', 'en-US')).toBe('$0.003');
  });
});
