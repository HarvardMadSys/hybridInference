// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getMyBatchCapabilities, type BatchCapabilities } from '@/lib/api/batches';

let userId: string | null = 'alice';

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: userId ? { id: userId } : null } }),
}));
vi.mock('@/lib/api/batches', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/batches')>()),
  getMyBatchCapabilities: vi.fn(),
}));

import { useBatchCapabilities } from './useBatchCapabilities';

function caps(owner: string, overrides: Partial<BatchCapabilities> = {}): BatchCapabilities {
  return {
    user_id: owner,
    status: 'enabled',
    enabled: true,
    min_role: 'internal',
    role_may_submit: false,
    has_history: true,
    history_error: false,
    show_entry: true,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

/** Let React Query deliver what it has batched (it notifies on a timer). */
async function flush() {
  await act(async () => {
    await new Promise((done) => setTimeout(done, 10));
  });
}

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, ...renderHook(() => useBatchCapabilities(), { wrapper }) };
}

beforeEach(() => {
  userId = 'alice';
  vi.mocked(getMyBatchCapabilities).mockReset();
});
afterEach(() => {
  cleanup();
  focusManager.setFocused(undefined);
});

describe('useBatchCapabilities', () => {
  it('is loading until the account has an answer, then returns it', async () => {
    vi.mocked(getMyBatchCapabilities).mockResolvedValue(caps('alice'));
    const { result } = setup();

    expect(result.current).toEqual({ capabilities: null, loading: true, error: null });
    await waitFor(() => expect(result.current.capabilities?.user_id).toBe('alice'));
    expect(result.current.loading).toBe(false);
  });

  it("never shows the previous account's slow answer to the next account", async () => {
    const slowAlice = deferred<BatchCapabilities>();
    const bob = deferred<BatchCapabilities>();
    vi.mocked(getMyBatchCapabilities)
      .mockReturnValueOnce(slowAlice.promise)
      .mockReturnValueOnce(bob.promise);
    const { result, rerender } = setup();

    userId = 'bob';
    rerender();
    // Alice's answer arrives after the switch, and Bob's has not.
    slowAlice.resolve(caps('alice', { show_entry: true }));
    await flush();
    expect(result.current.capabilities).toBeNull();
    expect(result.current.loading).toBe(true);

    bob.resolve(caps('bob', { show_entry: false, has_history: false }));
    await waitFor(() =>
      expect(result.current.capabilities).toMatchObject({ user_id: 'bob', show_entry: false }),
    );
  });

  it('rejects an answer for another account without leaving the page loading forever', async () => {
    vi.mocked(getMyBatchCapabilities).mockResolvedValue(caps('mallory'));
    const { result } = setup();

    await waitFor(() => expect(result.current.error).toBe('Batch access could not be verified.'));
    expect(result.current.capabilities).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('keeps the last known answer through a failed refresh, and reports the failure', async () => {
    vi.mocked(getMyBatchCapabilities)
      .mockResolvedValueOnce(caps('alice'))
      .mockRejectedValueOnce(new Error('Gateway timeout'));
    const { result, client } = setup();
    await waitFor(() => expect(result.current.capabilities?.show_entry).toBe(true));

    await act(async () => {
      await client.refetchQueries();
    });

    await waitFor(() => expect(result.current.error).toBe('Gateway timeout'));
    expect(result.current.capabilities?.show_entry).toBe(true);
  });

  it('reports a first failure without inventing an answer', async () => {
    vi.mocked(getMyBatchCapabilities).mockRejectedValue(new Error('offline'));
    const { result } = setup();

    await waitFor(() => expect(result.current.error).toBe('offline'));
    expect(result.current.capabilities).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("picks up the same account's configuration change when the tab regains focus", async () => {
    vi.mocked(getMyBatchCapabilities)
      .mockResolvedValueOnce(caps('alice', { status: 'enabled', show_entry: true }))
      .mockResolvedValueOnce(caps('alice', { status: 'disabled', show_entry: false }));
    const { result } = setup();
    await waitFor(() => expect(result.current.capabilities?.status).toBe('enabled'));

    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    await waitFor(() => expect(result.current.capabilities?.status).toBe('disabled'));
    expect(result.current.capabilities?.show_entry).toBe(false);
  });

  it('asks nothing while signed out', () => {
    userId = null;
    const { result } = setup();

    expect(result.current).toEqual({ capabilities: null, loading: false, error: null });
    expect(getMyBatchCapabilities).not.toHaveBeenCalled();
  });
});
