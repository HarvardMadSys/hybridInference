import { describe, expect, it } from 'vitest';

import type { RecentRequestItem } from '@/lib/api/user';
import { formatRouteWiseDecision } from './routewise';

const baseRequest: RecentRequestItem = {
  request_id: 'req-1',
  model_id: 'test-model',
  provider: 'openai',
  timestamp: '2026-05-13T00:00:00.000Z',
  routewise: {
    selected_tier: 'api',
    selected_provider: 'anthropic',
  },
};

describe('formatRouteWiseDecision', () => {
  it('omits hedge state when hedging_triggered is missing', () => {
    expect(formatRouteWiseDecision(baseRequest)).toBe('api: anthropic');
  });

  it('formats explicit hedge state', () => {
    expect(
      formatRouteWiseDecision({
        ...baseRequest,
        routewise: {
          ...baseRequest.routewise,
          hedging_triggered: true,
          hedge_backup_provider: 'openrouter',
        },
      }),
    ).toBe('api: anthropic; hedge -> openrouter');

    expect(
      formatRouteWiseDecision({
        ...baseRequest,
        routewise: {
          ...baseRequest.routewise,
          hedging_triggered: false,
        },
      }),
    ).toBe('api: anthropic; no hedge');
  });
});
