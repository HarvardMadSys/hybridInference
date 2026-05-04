'use client';

import { useEffect } from 'react';
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
        <div className="flex min-h-[50vh] flex-col items-center justify-center text-center">
          <h1 className="text-lg font-semibold text-gray-900">Admin access required</h1>
          <p className="mt-1 text-sm text-gray-500">
            You don&apos;t have permission to view this page.
          </p>
          <a
            href="/dashboard"
            className="mt-5 text-sm font-medium text-gray-900 underline decoration-gray-300 underline-offset-4 hover:decoration-gray-900 transition"
          >
            Back to Dashboard
          </a>
        </div>
      ) : (
        <div className="mx-auto w-full max-w-4xl pb-20">
          {/* Nav */}
          <div className="mb-10 flex items-center justify-between">
            <a
              href="/dashboard"
              className="group flex items-center gap-1.5 text-[13px] text-gray-400 transition hover:text-gray-900"
            >
              <svg
                className="h-3.5 w-3.5 transition group-hover:-translate-x-px"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2.5}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18"
                />
              </svg>
              Dashboard
            </a>
          </div>

          {/* Title */}
          <h1 className="text-[28px] font-bold tracking-tight text-gray-900">Admin</h1>
          <p className="mt-0.5 text-[15px] text-gray-500">
            Manage users, API keys, quotas, and audit log.
          </p>

          {/* Top-level tab toggle */}
          <AdminTabNav />

          {/* Tab content */}
          <main>{children}</main>
        </div>
      )}
    </ProtectedRoute>
  );
}
