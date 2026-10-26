// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { usePagedBatches } from './usePagedBatches';
import type { ConsoleBatch, ConsoleBatchPage } from '@/lib/api/batches';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function page(ids: string[], hasMore = false): ConsoleBatchPage {
  return {
    object: 'list',
    data: ids.map((id) => ({ id }) as unknown as ConsoleBatch),
    has_more: hasMore,
    last_id: ids.at(-1) ?? null,
  };
}

describe('usePagedBatches', () => {
  it('drops a next page that arrives after the query changed', async () => {
    const next = deferred<ConsoleBatchPage>();
    let query = 'a';
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) =>
        usePagedBatches(
          (after) => (after ? next.promise : Promise.resolve(page([`${query}-1`], query === 'a'))),
          key,
        ),
      { initialProps: { key: 'a' } },
    );
    await waitFor(() => expect(result.current.batches.map((b) => b.id)).toEqual(['a-1']));
    act(() => {
      void result.current.loadMore();
    });
    query = 'b';
    rerender({ key: 'b' });
    await waitFor(() => expect(result.current.batches.map((b) => b.id)).toEqual(['b-1']));
    await act(async () => {
      next.resolve(page(['a-2']));
      await next.promise;
    });
    expect(result.current.batches.map((b) => b.id)).toEqual(['b-1']);
  });

  it('clears the old list as soon as the query changes', async () => {
    const second = deferred<ConsoleBatchPage>();
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) =>
        usePagedBatches(() => (key === 'a' ? Promise.resolve(page(['a-1'])) : second.promise), key),
      { initialProps: { key: 'a' } },
    );
    await waitFor(() => expect(result.current.batches).toHaveLength(1));
    rerender({ key: 'b' });
    expect(result.current.batches).toEqual([]);
    expect(result.current.loading).toBe(true);
    await act(async () => {
      second.resolve(page(['b-1']));
      await second.promise;
    });
    expect(result.current.batches.map((b) => b.id)).toEqual(['b-1']);
  });
});
