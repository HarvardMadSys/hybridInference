'use client';

import { usePathname } from 'next/navigation';

/**
 * Max width of the console chrome (header, banner, main, footer) for the
 * current route.
 *
 * The user console (`/dashboard` and its pages) sets its navigation beside
 * the page, and its key and request tables need the room, so it gets a wider
 * frame. Other console pages read best at the default reading width. The admin area
 * is mostly wide data tables, so it gets a wider frame; its form-style tabs
 * lay out their cards and fields for that width rather than stretching them.
 * Header and footer use the same value so their edges line up with the page
 * below them.
 */
const CONSOLE_WIDTH_DEFAULT = 'max-w-5xl';
const CONSOLE_WIDTH_WIDE = 'max-w-[1440px]';
const CONSOLE_WIDTH_USER = 'max-w-7xl';

/** Chat is a viewport workspace; other console pages retain their reading width. */
export function isChatWorkspaceRoute(pathname: string): boolean {
  return pathname === '/dashboard/playground' || pathname === '/dashboard/playground/';
}

export function isWideConsoleRoute(pathname: string): boolean {
  return pathname === '/dashboard/admin' || pathname.startsWith('/dashboard/admin/');
}

/** The pages that show the user console's navigation (the `(console)` group). */
export function isUserConsoleRoute(pathname: string): boolean {
  if (pathname === '/dashboard') return true;
  if (!pathname.startsWith('/dashboard/')) return false;
  return !isWideConsoleRoute(pathname);
}

export function consoleWidthFor(pathname: string): string {
  if (isChatWorkspaceRoute(pathname)) return 'max-w-none';
  if (isWideConsoleRoute(pathname)) return CONSOLE_WIDTH_WIDE;
  return isUserConsoleRoute(pathname) ? CONSOLE_WIDTH_USER : CONSOLE_WIDTH_DEFAULT;
}

export function useConsoleWidth(): string {
  return consoleWidthFor(usePathname() ?? '/');
}
