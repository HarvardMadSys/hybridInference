import { describe, expect, it } from 'vitest';
import { parseLimit, readLimit } from './limits';

describe('a typed limit', () => {
  it('is an amount of zero or more, nothing, or not a limit at all', () => {
    expect(parseLimit('80')).toBe(80);
    expect(parseLimit(' 12.5 ')).toBe(12.5);
    expect(parseLimit('0')).toBe(0);
    expect(parseLimit('12.3456')).toBe(12.3456);
    expect(parseLimit('')).toBeNull();
    expect(parseLimit('   ')).toBeNull();
    expect(parseLimit('-1')).toBeUndefined();
    expect(parseLimit('ten')).toBeUndefined();
    expect(parseLimit('Infinity')).toBeUndefined();
  });

  it.each(['0x10', '1e3', '+5', '5.', '.5', '1,000', '12.34567'])(
    'is plain digits with at most four decimal places: not %j',
    (text) => {
      // Number() reads the first two as 16 and 1000.
      expect(parseLimit(text)).toBeUndefined();
    },
  );
});

describe('a limit field, read for a save', () => {
  it('is a change when the amount differs from the stored one', () => {
    expect(readLimit('95.5', 80, 'USD', 1000)).toEqual({ amount: 95.5, changed: true });
    expect(readLimit('', 80, 'USD', 1000)).toEqual({ amount: null, changed: true });
    expect(readLimit('0', null, 'USD', 1000)).toEqual({ amount: 0, changed: true });
  });

  it('is no change for the stored amount, also typed another way', () => {
    expect(readLimit('80', 80, 'USD', 1000)).toEqual({ amount: 80, changed: false });
    expect(readLimit(' 80.0 ', 80, 'USD', 1000)).toEqual({ amount: 80, changed: false });
    expect(readLimit('080', 80, 'USD', 1000)).toEqual({ amount: 80, changed: false });
    expect(readLimit(' ', null, 'USD', 1000)).toEqual({ amount: null, changed: false });
  });

  it('keeps a field left as it was read, whatever a new amount could be', () => {
    // More decimal places than a new amount may have: a migrated amount.
    expect(readLimit('80.123456', 80.123456, 'USD', 1000)).toEqual({
      amount: 80.123456,
      changed: false,
    });
    // Above a limit of the organization that was lowered after it was set.
    expect(readLimit('1500', 1500, 'USD', 1000)).toEqual({ amount: 1500, changed: false });
  });

  it('says what is wrong with an amount that cannot be saved', () => {
    expect(readLimit('eighty', 80, 'USD', 1000)).toEqual({ problem: 'invalid' });
    expect(readLimit('80.12345', 80, 'USD', 1000)).toEqual({ problem: 'invalid' });
    expect(readLimit('1000.01', 80, 'USD', 1000)).toEqual({ problem: 'above' });
    // The limit of the organization is the nearer bound, so it is the one named.
    expect(readLimit('1000000', 80, 'USD', 1000)).toEqual({ problem: 'above' });
  });

  it('has no ceiling without a limit of the organization, but the bound of the gateway in USD', () => {
    expect(readLimit('5000', 80, 'USD', null)).toEqual({ amount: 5000, changed: true });
    expect(readLimit('999999.9999', 80, 'USD', null)).toEqual({
      amount: 999999.9999,
      changed: true,
    });
    expect(readLimit('1000000', 80, 'USD', null)).toEqual({ problem: 'invalid' });
    // The gateway bounds the USD equivalent of an amount in another currency.
    expect(readLimit('1000000', 500, 'CNY', null)).toEqual({ amount: 1000000, changed: true });
  });
});
