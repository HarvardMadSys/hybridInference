'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import type { ConsoleBatch, ConsoleBatchPage } from '@/lib/api/batches';
import { getErrorMessage } from '@/lib/utils/errors';

interface PagedBatches {
  batches: ConsoleBatch[];
  hasMore: boolean;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  /** Load the first page again (the current query). */
  reload: () => Promise<void>;
  /** Append the next page; does nothing while a page is already loading. */
  loadMore: () => Promise<void>;
  /** Put an updated batch in place of the row with its id. */
  replace: (batch: ConsoleBatch) => void;
  /** Drop a batch from the list. */
  remove: (batchId: string) => void;
}

/**
 * A paged list of batches whose answers can never land in the wrong list.
 *
 * Each query (``queryKey``: a filter set, say) starts a new generation; a page
 * that arrives for an older generation is dropped, so a slow answer to the
 * previous filters cannot overwrite or extend the list for the current ones.
 * Only one "next page" request runs at a time, and rows already listed are not
 * appended twice. No next page is requested while the first page of a reload
 * is still loading.
 */
export function usePagedBatches(
  fetchPage: (after: string | null) => Promise<ConsoleBatchPage>,
  queryKey: string,
): PagedBatches {
  const [batches, setBatches] = useState<ConsoleBatch[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const cursor = useRef<string | null>(null);
  const moreInFlight = useRef(false);
  const reloading = useRef(false);
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;
  const rowsRef = useRef(batches);
  rowsRef.current = batches;
  const hasMoreRef = useRef(hasMore);
  hasMoreRef.current = hasMore;
  // The row whose next page came back as an empty end of the list. Until that
  // row's removal is confirmed the end is taken at its word; once it is, the
  // gateway had already deleted the row, so the end said nothing about the
  // rows after it (see remove).
  const endedAfter = useRef<string | null>(null);

  const reload = useCallback(async () => {
    const mine = ++generation.current;
    // Until the first page is back there is no valid cursor: a "next page"
    // asked for meanwhile would continue the old list after the new first page.
    cursor.current = null;
    endedAfter.current = null;
    reloading.current = true;
    moreInFlight.current = false;
    setLoadingMore(false);
    setHasMore(false);
    setLoading(true);
    setError(null);
    try {
      const page = await fetchRef.current(null);
      if (mine !== generation.current) return;
      setBatches(page.data);
      setHasMore(page.has_more);
      cursor.current = page.last_id;
    } catch (err) {
      if (mine !== generation.current) return;
      setError(getErrorMessage(err));
    } finally {
      if (mine === generation.current) {
        reloading.current = false;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    // A new query: forget the old list at once rather than showing it under
    // the new filters while the first page loads.
    setBatches([]);
    setHasMore(false);
    cursor.current = null;
    void reload();
  }, [queryKey, reload]);

  const loadMore = useCallback(async () => {
    if (reloading.current || moreInFlight.current || cursor.current === null) return;
    const mine = generation.current;
    moreInFlight.current = true;
    setLoadingMore(true);
    try {
      for (;;) {
        const after: string | null = cursor.current;
        const page = await fetchRef.current(after);
        if (mine !== generation.current) return;
        setError(null);
        if (cursor.current !== after && page.data.length === 0 && cursor.current !== null) {
          // The row this page was asked after was removed while it loaded
          // (see remove). The gateway finds the next page from that row, so
          // its empty answer says nothing about the rows after it: keep the
          // cursor remove moved to the new last row, and ask from there.
          continue;
        }
        setBatches((current) => {
          const seen = new Set(current.map((row) => row.id));
          return [...current, ...page.data.filter((row) => !seen.has(row.id))];
        });
        setHasMore(page.has_more);
        cursor.current = page.last_id;
        endedAfter.current = page.data.length === 0 && !page.has_more ? after : null;
        return;
      }
    } catch (err) {
      if (mine === generation.current) setError(getErrorMessage(err));
    } finally {
      if (mine === generation.current) {
        moreInFlight.current = false;
        setLoadingMore(false);
      }
    }
  }, []);

  const replace = useCallback((batch: ConsoleBatch) => {
    setBatches((current) => current.map((row) => (row.id === batch.id ? batch : row)));
  }, []);

  const remove = useCallback(
    (batchId: string) => {
      const rest = rowsRef.current.filter((row) => row.id !== batchId);
      // The next page is found from the cursor row; once that row is gone the
      // gateway ends the listing. Removed before its next page was asked for
      // (or while it loads), continue from the new last row: rows listed
      // twice are not appended again, and an empty page from the removed row
      // is asked again from there (see loadMore). Removed after its next page
      // already came back as that empty end, the list is resumed from the new
      // last row at once. With no row left, start over.
      const cursorRemoved = cursor.current === batchId && hasMoreRef.current;
      const endedOnRemoved = endedAfter.current === batchId;
      let resume = false;
      if ((cursorRemoved || endedOnRemoved) && !reloading.current) {
        if (rest.length === 0) {
          void reload();
          return;
        }
        cursor.current = rest[rest.length - 1].id;
        if (endedOnRemoved) {
          endedAfter.current = null;
          hasMoreRef.current = true;
          setHasMore(true);
          resume = true;
        }
      }
      rowsRef.current = rest;
      setBatches((current) => current.filter((row) => row.id !== batchId));
      if (resume) void loadMore();
    },
    [loadMore, reload],
  );

  return { batches, hasMore, loading, loadingMore, error, reload, loadMore, replace, remove };
}
