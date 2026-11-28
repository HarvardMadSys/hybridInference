import { useEffect } from 'react';

export const LEAVE_MESSAGE = 'Leave this page? Its unsaved changes will be lost.';

/**
 * While a page holds unsaved changes, leaving it asks first. A link to another
 * page of the console -- an admin tab, the side navigation -- is followed only
 * once the administrator confirms, and reloading or closing the window gets the
 * browser's own warning. Admin's tabs, and the Providers sections, are pages
 * of their own, so moving from Providers → Models to Keys or to Settings, or
 * anywhere else, would otherwise drop an edit unsaved.
 *
 * A link that leaves the page standing is not asked about: one to this same
 * page (a sub-tab, a filter), one that opens a new tab or downloads, and one
 * to another site, which the browser's own warning covers. Neither are the
 * browser's back and forward buttons: the App Router has no way to hold a
 * history step back.
 */
export function useLeaveGuard(unsaved: number): void {
  const guarding = unsaved > 0;
  useEffect(() => {
    if (!guarding) return undefined;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Some browsers still show their prompt only when this is set.
      event.returnValue = '';
    };
    // Captured on the document, before the console's links act on the click.
    const click = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
      if (!(anchor instanceof HTMLAnchorElement)) return;
      if ((anchor.target && anchor.target !== '_self') || anchor.hasAttribute('download')) return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin) return;
      if (url.pathname === window.location.pathname) return;
      if (!window.confirm(LEAVE_MESSAGE)) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    window.addEventListener('beforeunload', beforeUnload);
    document.addEventListener('click', click, true);
    return () => {
      window.removeEventListener('beforeunload', beforeUnload);
      document.removeEventListener('click', click, true);
    };
  }, [guarding]);
}
