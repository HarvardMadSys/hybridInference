// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

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

  it('clears a page error when retry succeeds and preserves the valid rows', async () => {
    const fetchPage = vi
      .fn()
      .mockResolvedValueOnce(page(['first'], true))
      .mockRejectedValueOnce(new Error('Filtering is temporarily unavailable'))
      .mockResolvedValueOnce(page(['second']));
    const { result } = renderHook(() => usePagedBatches(fetchPage, 'issues'));
    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['first']));

    await act(async () => result.current.loadMore());
    expect(result.current.error).toBe('Filtering is temporarily unavailable');
    expect(result.current.batches.map((row) => row.id)).toEqual(['first']);
    expect(result.current.hasMore).toBe(true);

    await act(async () => result.current.loadMore());
    expect(result.current.error).toBeNull();
    expect(result.current.batches.map((row) => row.id)).toEqual(['first', 'second']);
    expect(result.current.hasMore).toBe(false);
  });

  it('continues from the new last row after the last listed batch is removed', async () => {
    const fetchPage = vi
      .fn()
      .mockResolvedValueOnce(page(['a', 'b'], true))
      .mockResolvedValueOnce(page(['c']));
    const { result } = renderHook(() => usePagedBatches(fetchPage, 'mine'));
    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['a', 'b']));

    act(() => result.current.remove('b'));
    await act(async () => result.current.loadMore());
    expect(fetchPage).toHaveBeenLastCalledWith('a');
    expect(result.current.batches.map((row) => row.id)).toEqual(['a', 'c']);
  });

  it('starts over when every listed batch is removed', async () => {
    const fetchPage = vi
      .fn()
      .mockResolvedValueOnce(page(['a'], true))
      .mockResolvedValueOnce(page(['b']));
    const { result } = renderHook(() => usePagedBatches(fetchPage, 'mine'));
    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['a']));

    await act(async () => result.current.remove('a'));
    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['b']));
    expect(fetchPage).toHaveBeenLastCalledWith(null);
  });
  it('keeps the paging a removal set while a next page from the removed row was loading', async () => {
    const stale = deferred<ConsoleBatchPage>();
    const fetchPage = vi.fn(async (after: string | null) => {
      if (after === null) return page(['a', 'b'], true);
      if (after === 'b') return stale.promise;
      return page(['c', 'd'], true);
    });
    const { result } = renderHook(() => usePagedBatches(fetchPage, 'mine'));
    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['a', 'b']));

    let more!: Promise<void>;
    act(() => {
      more = result.current.loadMore();
    });
    expect(fetchPage).toHaveBeenLastCalledWith('b');
    // The last row is deleted while "Show more" is still loading from it ...
    act(() => result.current.remove('b'));
    // ... and the gateway, its cursor row gone, answers late with an empty end.
    await act(async () => {
      stale.resolve(page([], false));
      await more;
    });

    // The empty answer did not end the list: the next page came from the new last row.
    expect(fetchPage).toHaveBeenLastCalledWith('a');
    expect(result.current.batches.map((row) => row.id)).toEqual(['a', 'c', 'd']);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.loadingMore).toBe(false);
    await act(async () => result.current.loadMore());
    expect(fetchPage).toHaveBeenLastCalledWith('d');
  });

  it('still appends a next page that answered from the removed row before it was removed', async () => {
    const late = deferred<ConsoleBatchPage>();
    const fetchPage = vi.fn(async (after: string | null) =>
      after === null ? page(['a', 'b'], true) : late.promise,
    );
    const { result } = renderHook(() => usePagedBatches(fetchPage, 'mine'));
    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['a', 'b']));

    let more!: Promise<void>;
    act(() => {
      more = result.current.loadMore();
    });
    act(() => result.current.remove('b'));
    await act(async () => {
      late.resolve(page(['c'], false));
      await more;
    });

    expect(fetchPage).toHaveBeenCalledTimes(2);
    expect(result.current.batches.map((row) => row.id)).toEqual(['a', 'c']);
    expect(result.current.hasMore).toBe(false);
  });
  it('continues from the last remaining row when the empty page beat the delete confirmation', async () => {
    const fetchPage = vi.fn(async (after: string | null) => {
      if (after === null) return page(['a', 'b'], true);
      // The gateway already deleted "b": the page asked after it is an empty end.
      if (after === 'b') return page([], false);
      return page(['c', 'd'], true);
    });
    const { result } = renderHook(() => usePagedBatches(fetchPage, 'mine'));
    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['a', 'b']));

    await act(async () => result.current.loadMore());
    expect(fetchPage).toHaveBeenLastCalledWith('b');
    // Only now does the console hear that its delete of "b" succeeded.
    await act(async () => result.current.remove('b'));

    await waitFor(() =>
      expect(result.current.batches.map((row) => row.id)).toEqual(['a', 'c', 'd']),
    );
    expect(fetchPage).toHaveBeenLastCalledWith('a');
    expect(result.current.hasMore).toBe(true);
    await act(async () => result.current.loadMore());
    expect(fetchPage).toHaveBeenLastCalledWith('d');
  });

  it('starts over when the empty page beat the delete confirmation of the only row', async () => {
    let firstPage = page(['a'], true);
    const fetchPage = vi.fn(async (after: string | null) =>
      after === null ? firstPage : page([], false),
    );
    const { result } = renderHook(() => usePagedBatches(fetchPage, 'mine'));
    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['a']));

    await act(async () => result.current.loadMore());
    expect(fetchPage).toHaveBeenLastCalledWith('a');
    firstPage = page(['b', 'c'], true);
    await act(async () => result.current.remove('a'));

    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['b', 'c']));
    expect(fetchPage).toHaveBeenLastCalledWith(null);
    expect(result.current.hasMore).toBe(true);
  });

  it('keeps a real end of the list when an earlier row is removed', async () => {
    const fetchPage = vi.fn(async (after: string | null) =>
      after === null ? page(['a', 'b'], true) : page([], false),
    );
    const { result } = renderHook(() => usePagedBatches(fetchPage, 'mine'));
    await waitFor(() => expect(result.current.batches.map((row) => row.id)).toEqual(['a', 'b']));

    await act(async () => result.current.loadMore());
    await act(async () => result.current.remove('a'));

    expect(fetchPage).toHaveBeenCalledTimes(2);
    expect(result.current.batches.map((row) => row.id)).toEqual(['b']);
    expect(result.current.hasMore).toBe(false);
  });
});
