// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import * as api from '@/lib/api/organization';

vi.mock('@/lib/api/organization', () => ({
  getOrganization: vi.fn(),
  getOrganizationMember: vi.fn(),
  getOrganizationOverview: vi.fn(),
  listOrganizationAudit: vi.fn(),
  listOrganizationMemberUsage: vi.fn(),
  listOrganizationMembers: vi.fn(),
  listOrganizationModelUsage: vi.fn(),
  listOrganizationRequests: vi.fn(),
  removeOrganizationMember: vi.fn(),
  restoreOrganizationMember: vi.fn(),
  setDefaultMemberLimit: vi.fn(),
  setMemberMonthlyLimit: vi.fn(),
}));

import {
  useOrganization,
  useOrganizationMember,
  useOrganizationMembers,
  useOrganizationModelUsage,
  useOrganizationOverview,
  useRemoveOrganizationMember,
  useRestoreOrganizationMember,
  useSetDefaultMemberLimit,
  useSetMemberMonthlyLimit,
} from './useOrganization';

function wrapperFor(currency: 'USD' | 'CNY' = 'USD') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <SiteConfigProvider initialConfig={{ ...buildTimeSiteConfig, accounting: { currency } }}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </SiteConfigProvider>
  );
  return { client, wrapper };
}

beforeEach(() => {
  vi.mocked(api.getOrganization)
    .mockReset()
    .mockResolvedValue({ id: 'o-1' } as api.Organization);
  vi.mocked(api.getOrganizationOverview)
    .mockReset()
    .mockImplementation(async (period) => ({ period }) as api.OrganizationOverview);
  vi.mocked(api.listOrganizationMembers).mockReset().mockResolvedValue({ total: 0, members: [] });
  vi.mocked(api.getOrganizationMember)
    .mockReset()
    .mockResolvedValue({
      member: {},
      days: [],
      models: [],
      requests: [],
    } as unknown as api.MemberDetail);
  vi.mocked(api.listOrganizationModelUsage).mockReset().mockResolvedValue([]);
  vi.mocked(api.setMemberMonthlyLimit).mockReset().mockResolvedValue(undefined);
  vi.mocked(api.setDefaultMemberLimit)
    .mockReset()
    .mockResolvedValue({ id: 'o-1' } as api.Organization);
  vi.mocked(api.removeOrganizationMember).mockReset().mockResolvedValue(undefined);
  vi.mocked(api.restoreOrganizationMember).mockReset().mockResolvedValue(undefined);
});
afterEach(cleanup);

describe('reading the organization', () => {
  it('asks again for another period, and keeps the answer of the one before on screen', async () => {
    const { wrapper } = wrapperFor();
    const { result, rerender } = renderHook(
      ({ period }: { period: api.OrganizationPeriod }) => useOrganizationOverview(period),
      { wrapper, initialProps: { period: '7d' } },
    );
    await waitFor(() => expect(result.current.data?.period).toBe('7d'));

    rerender({ period: '30d' });

    // The page dims the figures it has instead of going blank.
    expect(result.current.data?.period).toBe('7d');
    expect(result.current.isPlaceholderData).toBe(true);
    await waitFor(() => expect(result.current.data?.period).toBe('30d'));
    expect(result.current.isPlaceholderData).toBe(false);
    expect(vi.mocked(api.getOrganizationOverview).mock.calls.map(([period]) => period)).toEqual([
      '7d',
      '30d',
    ]);
  });

  it('asks for the members with the filters it was given', async () => {
    const { wrapper } = wrapperFor();
    const query: api.MemberQuery = { period: '7d', status: 'removed', sortBy: 'spend', limit: 50 };
    const { result } = renderHook(() => useOrganizationMembers(query), { wrapper });

    await waitFor(() => expect(result.current.data).toEqual({ total: 0, members: [] }));
    expect(api.listOrganizationMembers).toHaveBeenCalledWith(query);
  });

  it('asks for one member by its id and the period', async () => {
    const { wrapper } = wrapperFor();
    const { result } = renderHook(() => useOrganizationMember('u-lei', '30d'), { wrapper });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(api.getOrganizationMember).toHaveBeenCalledWith('u-lei', '30d');
  });

  it('asks for nothing while a read is turned off', async () => {
    const { wrapper } = wrapperFor();
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useOrganizationModelUsage('7d', enabled),
      { wrapper, initialProps: { enabled: false } },
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(api.listOrganizationModelUsage).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await waitFor(() => expect(result.current.data).toEqual([]));
    expect(api.listOrganizationModelUsage).toHaveBeenCalledWith('7d');
  });

  it('keeps the answers of one ledger currency apart from those of another', async () => {
    const usd = wrapperFor('USD');
    const first = renderHook(() => useOrganization(), { wrapper: usd.wrapper });
    await waitFor(() => expect(first.result.current.data).toBeDefined());

    const keys = usd.client
      .getQueryCache()
      .getAll()
      .map((query) => query.queryKey);
    expect(keys).toEqual([['organization', 'record', 'USD']]);

    const cny = wrapperFor('CNY');
    const second = renderHook(() => useOrganization(), { wrapper: cny.wrapper });
    await waitFor(() => expect(second.result.current.data).toBeDefined());
    expect(
      cny.client
        .getQueryCache()
        .getAll()
        .map((query) => query.queryKey),
    ).toEqual([['organization', 'record', 'CNY']]);
  });
});

describe('changing the organization', () => {
  it('reads everything on screen again after a write, before the write is reported done', async () => {
    const { wrapper } = wrapperFor();
    const { result } = renderHook(
      () => ({
        record: useOrganization(),
        members: useOrganizationMembers({ period: '7d' }),
        overview: useOrganizationOverview('7d'),
        save: useSetMemberMonthlyLimit(),
      }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.overview.data).toBeDefined());
    await waitFor(() => expect(result.current.members.data).toBeDefined());
    await waitFor(() => expect(result.current.record.data).toBeDefined());

    await act(async () => {
      await result.current.save.mutateAsync({
        userId: 'u-lei',
        limit: { amount: 90, currency: 'USD' },
      });
    });

    expect(api.setMemberMonthlyLimit).toHaveBeenCalledWith('u-lei', {
      amount: 90,
      currency: 'USD',
    });
    // A limit changes the member's row, the organization's totals and the log together.
    expect(api.getOrganization).toHaveBeenCalledTimes(2);
    expect(api.listOrganizationMembers).toHaveBeenCalledTimes(2);
    expect(api.getOrganizationOverview).toHaveBeenCalledTimes(2);
  });

  it('passes each write to its call', async () => {
    const { wrapper } = wrapperFor();
    const { result } = renderHook(
      () => ({
        fallback: useSetDefaultMemberLimit(),
        remove: useRemoveOrganizationMember(),
        restore: useRestoreOrganizationMember(),
      }),
      { wrapper },
    );

    await act(async () => {
      await result.current.fallback.mutateAsync({ amount: null, currency: 'CNY' });
      await result.current.remove.mutateAsync('u-mei');
      await result.current.restore.mutateAsync('u-mei');
    });

    expect(api.setDefaultMemberLimit).toHaveBeenCalledWith({ amount: null, currency: 'CNY' });
    expect(api.removeOrganizationMember).toHaveBeenCalledWith('u-mei');
    expect(api.restoreOrganizationMember).toHaveBeenCalledWith('u-mei');
  });

  it('reads nothing again when the write is refused, and passes the refusal on', async () => {
    vi.mocked(api.removeOrganizationMember).mockRejectedValue(
      new Error('An administrator cannot be removed here.'),
    );
    const { wrapper } = wrapperFor();
    const { result } = renderHook(
      () => ({ record: useOrganization(), remove: useRemoveOrganizationMember() }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.record.data).toBeDefined());

    await act(async () => {
      await expect(result.current.remove.mutateAsync('u-yu')).rejects.toThrow(
        'An administrator cannot be removed here.',
      );
    });

    expect(api.getOrganization).toHaveBeenCalledTimes(1);
  });
});
