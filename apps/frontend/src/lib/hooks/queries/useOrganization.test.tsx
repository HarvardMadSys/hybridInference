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
  getMemberModels: vi.fn(),
  getOrganization: vi.fn(),
  getOrganizationMember: vi.fn(),
  getOrganizationMembersSummary: vi.fn(),
  getOrganizationOverview: vi.fn(),
  getOrganizationRequestVolume: vi.fn(),
  listOrganizationAudit: vi.fn(),
  listOrganizationMemberUsage: vi.fn(),
  listOrganizationMembers: vi.fn(),
  listOrganizationModelUsage: vi.fn(),
  listOrganizationRequests: vi.fn(),
  removeOrganizationMember: vi.fn(),
  restoreOrganizationMember: vi.fn(),
  revokeMemberKey: vi.fn(),
  setDefaultMemberLimit: vi.fn(),
  setMemberModels: vi.fn(),
  setMemberMonthlyLimit: vi.fn(),
}));

import {
  ORGANIZATION_QUERY_KEY,
  useOrganization,
  useOrganizationMember,
  useOrganizationMemberModels,
  useOrganizationMembers,
  useOrganizationMembersSummary,
  useOrganizationModelUsage,
  useOrganizationOverview,
  useOrganizationRequestVolume,
  useRemoveOrganizationMember,
  useRestoreOrganizationMember,
  useRevokeMemberKey,
  useSetDefaultMemberLimit,
  useSetMemberModels,
  useSetMemberMonthlyLimit,
} from './useOrganization';

const SUMMARY: api.MembersSummary = {
  near_limit: { count: 0, top: [] },
  top_spenders_today: { count: 0, top: [] },
};
const QUIET: api.RequestVolumeWindow = { requests: 0, ok: 0, failed: 0, avg_latency_ms: null };
const VOLUME: api.RequestVolume = { hour: QUIET, day: QUIET, week: QUIET, month: QUIET };
const MODELS: api.MemberModels = {
  available: ['glm-4.6'],
  disabled_models: [],
  platform_disabled_models: [],
};

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
  vi.mocked(api.getOrganizationMembersSummary).mockReset().mockResolvedValue(SUMMARY);
  vi.mocked(api.getOrganizationRequestVolume).mockReset().mockResolvedValue(VOLUME);
  vi.mocked(api.getMemberModels).mockReset().mockResolvedValue(MODELS);
  vi.mocked(api.setMemberModels).mockReset().mockResolvedValue(MODELS);
  vi.mocked(api.revokeMemberKey).mockReset().mockResolvedValue(undefined);
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

  it('asks for the two cards, the request volume and an hourly series', async () => {
    const { wrapper } = wrapperFor();
    const { result } = renderHook(
      () => ({
        summary: useOrganizationMembersSummary(),
        volume: useOrganizationRequestVolume(),
        hourly: useOrganizationOverview('7d', 'hour'),
        daily: useOrganizationOverview('7d'),
      }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.summary.data).toEqual(SUMMARY));
    await waitFor(() => expect(result.current.volume.data).toEqual(VOLUME));
    await waitFor(() => expect(result.current.daily.data).toBeDefined());
    // The series of hours and the series of days of one period are two answers.
    expect(api.getOrganizationOverview).toHaveBeenCalledWith('7d', 'hour');
    expect(api.getOrganizationOverview).toHaveBeenCalledWith('7d', undefined);
    expect(api.getOrganizationOverview).toHaveBeenCalledTimes(2);
  });

  it('asks for the models of a member only once it is named', async () => {
    const { wrapper } = wrapperFor();
    const { result, rerender } = renderHook(
      ({ userId }: { userId: string | null }) => useOrganizationMemberModels(userId),
      { wrapper, initialProps: { userId: null as string | null } },
    );
    expect(api.getMemberModels).not.toHaveBeenCalled();

    rerender({ userId: 'u-lei' });

    await waitFor(() => expect(result.current.data).toEqual(MODELS));
    expect(api.getMemberModels).toHaveBeenCalledWith('u-lei');
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
        models: useSetMemberModels(),
        revoke: useRevokeMemberKey(),
      }),
      { wrapper },
    );

    await act(async () => {
      await result.current.fallback.mutateAsync({ amount: null, currency: 'CNY' });
      await result.current.remove.mutateAsync('u-mei');
      await result.current.restore.mutateAsync('u-mei');
      await result.current.models.mutateAsync({ userId: 'u-lei', disabledModels: ['glm-4.6'] });
      await result.current.revoke.mutateAsync({ userId: 'u-lei', keyId: 41 });
    });

    expect(api.setDefaultMemberLimit).toHaveBeenCalledWith({ amount: null, currency: 'CNY' });
    expect(api.removeOrganizationMember).toHaveBeenCalledWith('u-mei');
    expect(api.restoreOrganizationMember).toHaveBeenCalledWith('u-mei');
    expect(api.setMemberModels).toHaveBeenCalledWith('u-lei', ['glm-4.6']);
    expect(api.revokeMemberKey).toHaveBeenCalledWith('u-lei', 41);
  });

  it.each([
    [
      'the models of a member',
      () => useSetMemberModels(),
      { userId: 'u-lei', disabledModels: ['glm-4.6'] },
    ],
    ['the revoking of a key', () => useRevokeMemberKey(), { userId: 'u-lei', keyId: 41 }],
  ] as const)(
    'reads every answer of the organization again after %s',
    async (_name, useWrite, variables) => {
      const { client, wrapper } = wrapperFor();
      const invalidate = vi.spyOn(client, 'invalidateQueries');
      const { result } = renderHook(
        () => ({
          member: useOrganizationMember('u-lei', '30d'),
          models: useOrganizationMemberModels('u-lei'),
          summary: useOrganizationMembersSummary(),
          write: useWrite(),
        }),
        { wrapper },
      );
      await waitFor(() => expect(result.current.member.data).toBeDefined());
      await waitFor(() => expect(result.current.models.data).toBeDefined());
      await waitFor(() => expect(result.current.summary.data).toBeDefined());

      await act(async () => {
        await (result.current.write.mutateAsync as (input: unknown) => Promise<unknown>)(variables);
      });

      // The whole prefix: the member, its models, the cards and the log change together.
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ORGANIZATION_QUERY_KEY });
      expect(ORGANIZATION_QUERY_KEY).toEqual(['organization']);
      expect(api.getOrganizationMember).toHaveBeenCalledTimes(2);
      expect(api.getMemberModels).toHaveBeenCalledTimes(2);
      expect(api.getOrganizationMembersSummary).toHaveBeenCalledTimes(2);
    },
  );

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
