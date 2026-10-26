'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import type { ConsoleBatch, ConsoleBatchPage } from '@/lib/api/batches';
import { getErrorMessage } from '@/lib/utils/errors';

export interface PagedBatches {
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

  const reload = useCallback(async () => {
    const mine = ++generation.current;
    // Until the first page is back there is no valid cursor: a "next page"
    // asked for meanwhile would continue the old list after the new first page.
    cursor.current = null;
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
      const page = await fetchRef.current(cursor.current);
      if (mine !== generation.current) return;
      setBatches((current) => {
        const seen = new Set(current.map((row) => row.id));
        return [...current, ...page.data.filter((row) => !seen.has(row.id))];
      });
      setHasMore(page.has_more);
      cursor.current = page.last_id;
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

  const remove = useCallback((batchId: string) => {
    setBatches((current) => current.filter((row) => row.id !== batchId));
  }, []);

  return { batches, hasMore, loading, loadingMore, error, reload, loadMore, replace, remove };
}
