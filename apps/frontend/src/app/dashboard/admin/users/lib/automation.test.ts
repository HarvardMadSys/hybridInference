import { describe, expect, it } from 'vitest';

import type { UserAutomationScore } from '@/lib/api/admin';
import { bandStyle, compareByScore } from './automation';

function scored(score: number): UserAutomationScore {
  return {
    user_id: 'u',
    days: 30,
    score,
    confidence: 0.5,
    band: 'mixed_or_uncertain',
    insufficient_data: false,
    n_req: 50,
    agent_share: 0,
    signals: {},
    detail: {},
  };
}

describe('bandStyle', () => {
  it('maps known bands and falls back to mixed for unknown', () => {
    expect(bandStyle('likely_human').label).toBe('Human');
    expect(bandStyle('scripted_batch').label).toBe('Script');
    expect(bandStyle('totally-unknown').label).toBe(bandStyle('mixed_or_uncertain').label);
  });
});

describe('compareByScore', () => {
  it('orders by score honoring the direction', () => {
    expect(compareByScore(scored(0.9), scored(0.1), 'desc')).toBeLessThan(0);
    expect(compareByScore(scored(0.9), scored(0.1), 'asc')).toBeGreaterThan(0);
  });

  it('sorts users without a score to the end regardless of direction', () => {
    expect(compareByScore(undefined, scored(0.5), 'desc')).toBeGreaterThan(0);
    expect(compareByScore(scored(0.5), undefined, 'desc')).toBeLessThan(0);
    expect(compareByScore(undefined, scored(0.5), 'asc')).toBeGreaterThan(0);
    expect(compareByScore(scored(0.5), undefined, 'asc')).toBeLessThan(0);
    expect(compareByScore(undefined, undefined, 'desc')).toBe(0);
  });
});
