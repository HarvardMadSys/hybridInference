import { describe, expect, it, vi } from 'vitest';

// Next's redirect never returns: it throws, and the router follows it.
const redirectMock = vi.hoisted(() =>
  vi.fn((url: string) => {
    throw new Error(`NEXT_REDIRECT ${url}`);
  }),
);

vi.mock('next/navigation', () => ({ redirect: redirectMock }));

import Page from './page';

describe('the old Admin → Models address', () => {
  it('lands on Providers → Models, where the model configuration moved', () => {
    expect(() => Page()).toThrow('NEXT_REDIRECT /dashboard/admin/providers/models');
  });
});
