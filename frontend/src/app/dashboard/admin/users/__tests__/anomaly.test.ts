import { describe, expect, it } from 'vitest';
import { isAnomalous } from '../lib/anomaly';

describe('isAnomalous', () => {
  it('flags 5x spike with $1+ today and 7d history', () => {
    const history = [1, 1, 1, 1, 1, 1, 1]; // avg = 1
    expect(isAnomalous(5.01, history)).toBe(true);
  });

  it('does not flag exactly 5x today (strict >)', () => {
    const history = [1, 1, 1, 1, 1, 1, 1];
    expect(isAnomalous(5.0, history)).toBe(true); // spec uses >=
  });

  it('does not flag below $1 floor', () => {
    const history = [0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05]; // avg = 0.05
    // today = 0.99 is 19.8x avg but below $1 floor
    expect(isAnomalous(0.99, history)).toBe(false);
  });

  it('does not flag fewer than 3 days of history', () => {
    expect(isAnomalous(100, [1, 1])).toBe(false);
    expect(isAnomalous(100, [])).toBe(false);
  });

  it('does not flag when prior average is zero', () => {
    expect(isAnomalous(10, [0, 0, 0, 0])).toBe(false);
  });

  it('returns multiplier when anomalous', () => {
    const history = [1, 1, 1, 1, 1, 1, 1];
    expect(isAnomalous(10, history, { returnMultiplier: true })).toBe(10);
  });
});
