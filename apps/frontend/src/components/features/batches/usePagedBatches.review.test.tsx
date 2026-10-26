// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import type { ConsoleBatch, ConsoleBatchPage } from '@/lib/api/batches';
import { usePagedBatches } from './usePagedBatches';

function page(id: string): ConsoleBatchPage {
  return { object: 'list', data: [{ id } as unknown as ConsoleBatch], has_more: true, last_id: id };
}

afterEach(cleanup);

it('does not use the previous page cursor while Refresh is loading the first page', async () => {
  let finishReload!: (value: ConsoleBatchPage) => void;
  const reload = new Promise<ConsoleBatchPage>((resolve) => {
    finishReload = resolve;
  });
  const fetchPage = vi
    .fn()
    .mockResolvedValueOnce(page('page1'))
    .mockResolvedValueOnce(page('page2'))
    .mockReturnValueOnce(reload)
    .mockResolvedValue(page('page3'));
  const { result } = renderHook(() => usePagedBatches(fetchPage, 'all'));
  await waitFor(() => expect(result.current.batches).toHaveLength(1));
  await act(async () => {
    await result.current.loadMore();
  });
  expect(result.current.batches.map((row) => row.id)).toEqual(['page1', 'page2']);

  let reloadDone!: Promise<void>;
  act(() => {
    reloadDone = result.current.reload();
  });
  await act(async () => {
    await result.current.loadMore();
  });
  const readsBeforeRefreshCompletes = fetchPage.mock.calls.map((args) => args[0]);
  await act(async () => {
    finishReload(page('page1'));
    await reloadDone;
  });
  expect(readsBeforeRefreshCompletes).toEqual([null, 'page1', null]);
  expect(result.current.batches.map((row) => row.id)).toEqual(['page1']);
});
