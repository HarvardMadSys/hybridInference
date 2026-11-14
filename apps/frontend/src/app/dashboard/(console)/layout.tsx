'use client';

import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { ConsoleNav } from '@/components/features/dashboard/ConsoleNav';
import { isChatWorkspaceRoute } from '@/components/ui/consoleWidth';
import { usePathname } from 'next/navigation';

/**
 * The user console: its navigation beside every page in this group.
 *
 * A route group, so the URLs stay `/dashboard/...`. The admin area sits
 * outside it because it brings its own chrome.
 */
export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const workspace = isChatWorkspaceRoute(usePathname() ?? '');
  return (
    <ProtectedRoute>
      <div
        className={
          workspace
            ? 'flex min-h-0 w-full flex-1 flex-col overflow-hidden md:flex-row'
            : 'flex w-full flex-col gap-6 md:flex-row md:gap-8'
        }
      >
        <ConsoleNav workspace={workspace} />
        <div className={workspace ? 'flex min-h-0 min-w-0 flex-1 flex-col' : 'min-w-0 flex-1'}>
          {children}
        </div>
      </div>
    </ProtectedRoute>
  );
}
