'use client';

import { usePathname } from 'next/navigation';

/**
 * How the console chrome frames the current route.
 *
 * The user console (`/dashboard` and its pages) is an application shell: a bar
 * across the top of the window, the navigation down its left edge and the page
 * beside it. The admin area has the same bar, so moving between the two does
 * not move the name or the account menu, and lays its wide tables out below
 * it. Every other page (sign-in, stats, legal text) is centred at the reading
 * width under a header of that width.
 */
const CONSOLE_WIDTH_DEFAULT = 'max-w-5xl';
const CONSOLE_WIDTH_WIDE = 'max-w-[1440px]';

/**
 * Max width of a page beside the user console's navigation: the room its key
 * and request tables are laid out for, centred in what a wide window leaves.
 */
export const CONSOLE_PAGE_WIDTH = 'max-w-[68rem]';

export function isDashboardRoute(pathname: string): boolean {
  return pathname === '/dashboard' || pathname.startsWith('/dashboard/');
}

/** Chat fills the window below the bar and scrolls its own panes. */
export function isChatWorkspaceRoute(pathname: string): boolean {
  return pathname === '/dashboard/playground' || pathname === '/dashboard/playground/';
}

export function isWideConsoleRoute(pathname: string): boolean {
  return pathname === '/dashboard/admin' || pathname.startsWith('/dashboard/admin/');
}

/** The pages that show the user console's navigation (the `(console)` group). */
export function isUserConsoleRoute(pathname: string): boolean {
  return isDashboardRoute(pathname) && !isWideConsoleRoute(pathname);
}

/**
 * Max width of the banner and footer, and of `main` outside the user console,
 * whose page is `CONSOLE_PAGE_WIDTH` wide beside the navigation.
 */
export function consoleWidthFor(pathname: string): string {
  if (isUserConsoleRoute(pathname)) return 'max-w-none';
  if (isWideConsoleRoute(pathname)) return CONSOLE_WIDTH_WIDE;
  return CONSOLE_WIDTH_DEFAULT;
}

export function useConsoleWidth(): string {
  return consoleWidthFor(usePathname() ?? '/');
}

export interface ConsoleFrame {
  /** The top bar spans the window: every dashboard route. */
  bar: boolean;
  /** The user console's shell: its navigation, with the page beside it. */
  shell: boolean;
  /** The page is as tall as the window and scrolls inside itself: Chat. */
  workspace: boolean;
}

export function consoleFrameFor(pathname: string): ConsoleFrame {
  return {
    bar: isDashboardRoute(pathname),
    shell: isUserConsoleRoute(pathname),
    workspace: isChatWorkspaceRoute(pathname),
  };
}

export function useConsoleFrame(): ConsoleFrame {
  return consoleFrameFor(usePathname() ?? '/');
}
