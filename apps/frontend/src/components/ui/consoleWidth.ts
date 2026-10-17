'use client';

import { usePathname } from 'next/navigation';

/**
 * Max width of the console chrome (header, banner, main, footer) for the
 * current route.
 *
 * Most console pages read best at the default reading width. The admin area
 * is mostly wide data tables, so it gets a wider frame; its form-style pages
 * narrow themselves inside it (see `ADMIN_FORM_WIDTH`). Header and footer use
 * the same value so their edges line up with the page below them.
 */
export const CONSOLE_WIDTH_DEFAULT = 'max-w-5xl';
export const CONSOLE_WIDTH_WIDE = 'max-w-[1440px]';

export function isWideConsoleRoute(pathname: string): boolean {
  return pathname === '/dashboard/admin' || pathname.startsWith('/dashboard/admin/');
}

export function useConsoleWidth(): string {
  const pathname = usePathname() ?? '/';
  return isWideConsoleRoute(pathname) ? CONSOLE_WIDTH_WIDE : CONSOLE_WIDTH_DEFAULT;
}
