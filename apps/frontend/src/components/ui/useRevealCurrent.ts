import { useEffect } from 'react';

import { revealHorizontally } from './revealHorizontally';

/**
 * Keeps the current link of a row that scrolls sideways in view, on the first
 * load and after every navigation: the link marked `aria-current="page"`.
 * Admin's tab bar and the Providers section control use it on a phone, where
 * neither fits the screen.
 */
export function useRevealCurrent(
  scrollerRef: { readonly current: HTMLElement | null },
  pathname: string,
): void {
  useEffect(() => {
    const scroller = scrollerRef.current;
    const current = scroller?.querySelector<HTMLElement>('[aria-current="page"]');
    if (scroller && current) revealHorizontally(scroller, current);
  }, [scrollerRef, pathname]);
}
