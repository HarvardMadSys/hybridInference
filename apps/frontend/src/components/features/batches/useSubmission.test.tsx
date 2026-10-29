// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { IntentStore, SubmitIntent } from '@/lib/batches/submitIntent';

import { useSubmission } from './useSubmission';

const mocks = vi.hoisted(() => ({ get: vi.fn(), clear: vi.fn(), settle: vi.fn() }));

vi.mock('@/lib/batches/submitIntent', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/batches/submitIntent')>()),
  withIntentStore: (_owner: string, run: (store: IntentStore) => Promise<void>) =>
    run({ get: mocks.get, clear: mocks.clear } as unknown as IntentStore),
  settleIntent: mocks.settle,
}));

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('submission settlement across account changes', () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.get.mockImplementation(async (owner: string) =>
      owner === 'owner-a' ? ({ key: 'pending-a' } as SubmitIntent) : null,
    );
    mocks.settle.mockResolvedValue({ kind: 'created', batch: { id: 'batch-a' } });
  });

  it('does not publish the previous account outcome after its pending clear finishes', async () => {
    const clearing = deferred();
    mocks.clear.mockReturnValue(clearing.promise);
    const { result, rerender } = renderHook(({ owner }) => useSubmission(owner), {
      initialProps: { owner: 'owner-a' },
    });
    await waitFor(() => expect(mocks.clear).toHaveBeenCalledWith('owner-a', 'pending-a'));

    rerender({ owner: 'owner-b' });
    await waitFor(() => expect(result.current.state.phase).toBe('idle'));
    await act(async () => {
      clearing.resolve();
      await clearing.promise;
    });

    expect(result.current.state).toEqual({ phase: 'idle' });
  });

  it('publishes the outcome when the same account remains active', async () => {
    const clearing = deferred();
    mocks.clear.mockReturnValue(clearing.promise);
    const { result } = renderHook(() => useSubmission('owner-a'));
    await waitFor(() => expect(mocks.clear).toHaveBeenCalledWith('owner-a', 'pending-a'));
    await act(async () => {
      clearing.resolve();
      await clearing.promise;
    });

    expect(result.current.state).toEqual({ phase: 'created', batch: { id: 'batch-a' } });
  });
});
