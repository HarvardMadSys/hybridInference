import { describe, expect, it } from 'vitest';

import {
  compactNumber,
  durationText,
  estimatedFinish,
  sharePercent,
  summaryOutcomeCounts,
  usd,
} from './batchUsage';

describe('usd', () => {
  it('shows cents from a dollar up, and small costs to four decimals', () => {
    expect(usd(0)).toBe('$0.00');
    expect(usd(0.0012)).toBe('$0.0012');
    expect(usd(0.25)).toBe('$0.2500');
    expect(usd(1234.567)).toBe('$1,234.57');
  });
});

describe('compactNumber', () => {
  it('is exact below ten thousand and shortened above', () => {
    expect(compactNumber(9_999)).toBe('9,999');
    expect(compactNumber(12_900)).toBe('12.9K');
    expect(compactNumber(4_200_000)).toBe('4.2M');
  });
});

describe('sharePercent', () => {
  it('never rounds to 100% or 0% across a remainder', () => {
    expect(sharePercent(590_000, 592_445)).toBe('99.6%');
    expect(sharePercent(999_999, 1_000_000)).toBe('99.9%');
    expect(sharePercent(1, 1_000_000)).toBe('0.1%');
    expect(sharePercent(5, 5)).toBe('100%');
    expect(sharePercent(0, 5)).toBe('0%');
    expect(sharePercent(0, 0)).toBe('—');
  });
});

describe('summaryOutcomeCounts', () => {
  const usage = {
    batches: 1,
    active: 0,
    requests: 10,
    completed: 5,
    failed: 3,
    cancelled: 1,
    cost_usd: 0,
    prompt_tokens: 0,
    completion_tokens: 0,
    unknown_cost_attempts: 0,
  };

  it('takes expired requests out of failed', () => {
    expect(summaryOutcomeCounts({ ...usage, expired: 2 })).toEqual({
      succeeded: 5,
      failed: 1,
      expired: 2,
      cancelled: 1,
      unfinished: 1,
      total: 10,
    });
  });

  it('leaves failed whole when the gateway does not report expired', () => {
    expect(summaryOutcomeCounts(usage)).toMatchObject({ failed: 3, expired: 0, unfinished: 1 });
  });
});

describe('estimatedFinish', () => {
  const now = 1_000_000;
  const running = {
    status: 'in_progress' as const,
    request_counts: { total: 100, completed: 30, failed: 5 },
    cancelled: 5,
    started_at: now - 400,
    expires_at: now + 86_400,
    paused_until: null,
  };

  it('extrapolates the pace so far over what is left', () => {
    // 40 done in 400 s, 60 left: 600 s more.
    expect(estimatedFinish(running, now)).toEqual({ at: now + 600, in: 600, afterWindow: false });
  });

  it('says when the window closes first', () => {
    expect(estimatedFinish({ ...running, expires_at: now + 300 }, now)?.afterWindow).toBe(true);
  });

  it('gives no estimate without a pace or while not running', () => {
    const none = {
      ...running,
      request_counts: { total: 100, completed: 0, failed: 0 },
      cancelled: 0,
    };
    expect(estimatedFinish(none, now)).toBeNull();
    expect(estimatedFinish({ ...running, status: 'validating' }, now)).toBeNull();
    expect(estimatedFinish({ ...running, started_at: null }, now)).toBeNull();
    expect(estimatedFinish({ ...running, paused_until: now + 60 }, now)).toBeNull();
    const done = { ...running, request_counts: { total: 40, completed: 30, failed: 5 } };
    expect(estimatedFinish(done, now)).toBeNull();
  });
});

describe('durationText', () => {
  it('reads to the minute', () => {
    expect(durationText(20)).toBe('1 min');
    expect(durationText(45 * 60)).toBe('45 min');
    expect(durationText(2 * 3600)).toBe('2 h');
    expect(durationText(2 * 3600 + 5 * 60)).toBe('2 h 5 min');
    expect(durationText(3 * 86_400 + 4 * 3600 + 120)).toBe('3 d 4 h');
  });
});
