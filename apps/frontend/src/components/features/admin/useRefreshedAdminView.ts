'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { getErrorMessage } from '@/lib/utils/errors';

/** What every server-kept admin view carries about its own age. */
export interface RefreshedAdminView {
  /** When the server computed it; null while its first computation runs. */
  generated_at: string | null;
  /** Whether the server is computing a new one now. */
  refreshing: boolean;
  /** Why the server's last computation failed, if none has succeeded since. */
  refresh_error?: string | null;
}

export const REFRESH_POLL_MS = 3_000;
// Ten minutes of polling, well past any computation that will finish.
const MAX_POLLS = 200;

export const REFRESH_DID_NOT_COMPLETE =
  'The refresh did not complete; showing the earlier results.';

export function refreshFailedMessage(reason: string): string {
  return `The last refresh failed (${reason}); showing the earlier results.`;
}

/**
 * One server-kept admin view (the Providers tab's percentiles and TTFT
 * scatter): fetched, and polled while the server recomputes it.
 *
 * The server answers at once with the result it has, flagged `refreshing`
 * while a new one is computed -- after `load(true)`, or when it found its
 * result stale. Until the flag clears this asks again every few seconds and
 * shows each answer, so the page shows the previous numbers, then the new ones.
 * A failed computation is reported as an error, with the earlier numbers still
 * shown: the server says why (`refresh_error`), and a refresh asked for here
 * that ends with the same `generated_at` failed too.
 */
export function useRefreshedAdminView<T extends RefreshedAdminView>(
  fetchView: (opts: { refresh?: boolean }) => Promise<T>,
) {
  const [data, setData] = useState<T | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Each load bumps the generation; an older load's answers and polls stop.
  const generation = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopPolling = () => {
    if (timer.current !== null) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  };

  useEffect(
    () => () => {
      generation.current += 1;
      stopPolling();
    },
    [],
  );

  const load = useCallback(
    async (refresh = false) => {
      const own = ++generation.current;
      stopPolling();
      setBusy(true);
      setError(null);
      // The result a refresh replaces: what the first answer showed while the
      // server reported recomputing it.
      let replacing: string | null | undefined;

      const ask = async (polls: number, askRefresh: boolean) => {
        try {
          const view = await fetchView({ refresh: askRefresh });
          if (own !== generation.current) return;
          if (polls === 0 && view.refreshing) replacing = view.generated_at;
          setData(view);
          if (view.refreshing && polls < MAX_POLLS) {
            timer.current = setTimeout(() => void ask(polls + 1, false), REFRESH_POLL_MS);
            return;
          }
          if (view.refresh_error) {
            setError(refreshFailedMessage(view.refresh_error));
          } else if (refresh && replacing !== undefined && view.generated_at === replacing) {
            setError(REFRESH_DID_NOT_COMPLETE);
          }
          setBusy(false);
        } catch (e) {
          if (own !== generation.current) return;
          setError(getErrorMessage(e));
          setBusy(false);
        }
      };
      await ask(0, refresh);
    },
    [fetchView],
  );

  return { data, busy, error, load };
}
