'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/components/providers';

export function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { state } = useAuth();

  useEffect(() => {
    if (!state.loading && !state.isAuthenticated) {
      router.push('/login');
    }
  }, [state.loading, state.isAuthenticated, router]);

  if (state.loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
      </div>
    );
  }

  if (!state.isAuthenticated) {
    return null;
  }

  return <>{children}</>;
}
