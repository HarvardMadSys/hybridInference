'use client';

import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';

/**
 * The user console's pages, for a signed-in user.
 *
 * The console chrome draws their navigation and frame (`ConsoleChrome`), so
 * that a page which fails to render is still shown inside them. The admin
 * area sits outside this group because it brings its own tabs.
 */
export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  return <ProtectedRoute>{children}</ProtectedRoute>;
}
