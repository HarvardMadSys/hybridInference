// @vitest-environment jsdom
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { listUsers } from '@/lib/api/admin';

import { useUsers } from '../hooks/useUsers';
import { DEFAULT_FILTER_STATE } from '../lib/filterTypes';
import type { FilterState } from '../types';

vi.mock('@/lib/hooks/useMoney', () => ({ useMoney: () => ({ currency: 'CNY' }) }));
vi.mock('@/lib/api/admin', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/admin')>()),
  listUsers: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.mocked(listUsers).mockReset();
});

function render(state: FilterState) {
  vi.mocked(listUsers).mockResolvedValue({
    total: 0,
    users: [],
    status_counts: {
      all: 0,
      pending_approval: 0,
      active: 0,
      suspended: 0,
      rejected: 0,
      deleted: 0,
    },
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => useUsers(state), { wrapper });
}

describe('useUsers', () => {
  it('sends the selected account tier', async () => {
    render({ ...DEFAULT_FILTER_STATE, role: 'admin' });
    await waitFor(() => expect(listUsers).toHaveBeenCalledTimes(1));
    expect(vi.mocked(listUsers).mock.calls[0][0]).toMatchObject({ role: 'admin' });
  });

  it('names no currency without a spend threshold', async () => {
    // The gateway refuses a request in another currency than its ledger's, so a
    // list without thresholds must not depend on the one the page was loaded under.
    render({ ...DEFAULT_FILTER_STATE, status: 'active', provider: 'openai' });
    await waitFor(() => expect(listUsers).toHaveBeenCalledTimes(1));
    const options = vi.mocked(listUsers).mock.calls[0][0];
    expect(options?.currency).toBeUndefined();
    expect(options).toMatchObject({ status: 'active', provider: 'openai' });
  });

  it.each([
    ['today', { minCostToday: 30 }],
    ['this month', { minCostMonth: 300 }],
  ])('sends a threshold for %s with the currency it was typed in', async (_label, threshold) => {
    render({ ...DEFAULT_FILTER_STATE, ...threshold });
    await waitFor(() => expect(listUsers).toHaveBeenCalledTimes(1));
    expect(vi.mocked(listUsers).mock.calls[0][0]).toMatchObject({ currency: 'CNY', ...threshold });
  });
});
