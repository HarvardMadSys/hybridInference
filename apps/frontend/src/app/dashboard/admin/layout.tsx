'use client';

import { useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { useAuth } from '@/components/providers';
import { AdminTabNav } from '@/components/features/admin/AdminTabNav';

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { state } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!state.loading && state.isAuthenticated && !state.user?.is_admin) {
      router.replace('/dashboard');
    }
  }, [state.loading, state.isAuthenticated, state.user, router]);

  return (
    <ProtectedRoute>
      {!state.user?.is_admin ? (
        <div
          lang="en"
          className="flex min-h-[50vh] flex-col items-center justify-center text-center"
        >
          <h1 className="text-lg font-semibold text-gray-900">Admin access required</h1>
          <p className="mt-1 text-sm text-gray-500">
            You don&apos;t have permission to view this page.
          </p>
          <Link
            href="/dashboard"
            className="mt-5 text-sm font-medium text-gray-900 underline decoration-gray-300 underline-offset-4 hover:decoration-gray-900 transition"
          >
            Back to Dashboard
          </Link>
        </div>
      ) : (
        // Administration is not translated: it is for the operators, not the
        // console's users, and stays in English whatever language the reader
        // chose. `lang` says so to screen readers and browser translation.
        <div lang="en" className="w-full pb-20">
          {/* One short line of chrome: where you are and the way back, so
              the tab's own content starts near the top of the window. */}
          <header className="flex items-center gap-2">
            <Link
              href="/dashboard"
              className="group flex items-center gap-1 rounded text-[13px] text-gray-500 transition hover:text-gray-900"
            >
              <svg
                className="h-3.5 w-3.5 transition group-hover:-translate-x-px"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2.5}
                aria-hidden="true"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18"
                />
              </svg>
              Dashboard
            </Link>
            <span aria-hidden="true" className="text-[13px] text-gray-300">
              /
            </span>
            <h1 className="text-[18px] font-semibold tracking-tight text-gray-900">Admin</h1>
          </header>

          {/* Top-level tab toggle */}
          <AdminTabNav />

          {/* Tab content */}
          <main>{children}</main>
        </div>
      )}
    </ProtectedRoute>
  );
}
