'use client';

import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { ConsoleNav } from '@/components/features/dashboard/ConsoleNav';

/**
 * The user console: its navigation beside every page in this group.
 *
 * A route group, so the URLs stay `/dashboard/...`. The admin area and the
 * Playground sit outside it because they bring their own chrome.
 */
export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  return (
    <ProtectedRoute>
      <div className="flex w-full flex-col gap-6 md:flex-row md:gap-8">
        <ConsoleNav />
        <div className="min-w-0 flex-1">{children}</div>
      </div>
    </ProtectedRoute>
  );
}
