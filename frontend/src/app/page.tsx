'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/components/providers';

export default function HomePage() {
  const router = useRouter();
  const { state } = useAuth();

  useEffect(() => {
    if (state.loading) return;
    router.replace(state.isAuthenticated ? '/dashboard' : '/login');
  }, [state.loading, state.isAuthenticated, router]);

  return (
    <div className="flex w-full items-center justify-center">
      <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
    </div>
  );
}
