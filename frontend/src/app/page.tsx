'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { CodeExample, Features, Hero, HowItWorks } from '@/components/landing';

export default function HomePage(): JSX.Element {
  const router = useRouter();
  const { state } = useAuth();

  useEffect(() => {
    if (state.loading) return;
    if (state.isAuthenticated) {
      router.replace('/dashboard');
    }
  }, [state.loading, state.isAuthenticated, router]);

  if (state.loading || state.isAuthenticated) {
    return (
      <div className="flex w-full items-center justify-center">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-crimson"></div>
      </div>
    );
  }

  return (
    <div className="flex w-full flex-col gap-4">
      <Hero />
      <Features />
      <HowItWorks />
      <CodeExample />
    </div>
  );
}
