'use client';

import { useSearchParams } from 'next/navigation';
import { useEffect, useState } from 'react';

/**
 * The `token` an emailed link put in this page's address: read once, then taken
 * out of the address.
 *
 * A password-reset or email-verification link carries a live credential in its
 * query, and left in the address it goes wherever the address goes: the Referer
 * of the page's own requests, which the gateway logs; the browser's history; a
 * shared screen. So the page keeps the token in memory and removes it from the
 * address (and from this history entry) as soon as it has read it; a reload
 * asks for the link again. The pages are also served with
 * `Referrer-Policy: strict-origin` (next.config.js), for what they request
 * before this runs.
 *
 * Read in the first render, not from `useSearchParams` afterwards: taking the
 * token out changes the search parameters, and the page must not then decide
 * the link had none.
 */
export function useLinkToken(): string | null {
  const searchParams = useSearchParams();
  const [token] = useState(() => searchParams.get('token'));

  useEffect(() => {
    if (!token) return;
    const url = new URL(window.location.href);
    if (!url.searchParams.has('token')) return;
    url.searchParams.delete('token');
    // `null`, not the current state: Next's router then takes up the new
    // address, and does not write the old one back on its next update.
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  }, [token]);

  return token;
}
