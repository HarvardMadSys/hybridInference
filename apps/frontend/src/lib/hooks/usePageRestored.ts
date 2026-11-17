'use client';

import { useEffect, useRef } from 'react';

/**
 * Run `restore` when the browser shows this page again from its back-forward
 * cache.
 *
 * A page that leaves for another site keeps its button busy until it is gone.
 * Going back can bring the same document out of that cache exactly as it was
 * left: the button still busy, and nothing that would ever end it.
 */
export function usePageRestored(restore: () => void): void {
  const latest = useRef(restore);
  useEffect(() => {
    latest.current = restore;
  });
  useEffect(() => {
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) latest.current();
    };
    window.addEventListener('pageshow', onPageShow);
    return () => window.removeEventListener('pageshow', onPageShow);
  }, []);
}
