'use client';

import dynamic from 'next/dynamic';
import { useAuth } from '@/components/providers';
import { CodeExample, Features, Hero, HowItWorks } from '@/components/landing';

const DashboardView = dynamic(() =>
  import('@/components/features/dashboard/DashboardView').then((mod) => mod.DashboardView),
);

export default function HomePage(): JSX.Element {
  const { state } = useAuth();

  if (state.loading) {
    return (
      <div className="flex w-full items-center justify-center">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-crimson"></div>
      </div>
    );
  }

  if (state.isAuthenticated) {
    return (
      <div className="flex w-full flex-col gap-4">
        <DashboardView />
        <footer className="px-4 pb-6 text-center text-xs text-gray-500 sm:px-6 lg:px-8">
          <p>Service is provided without guarantee.</p>
          <p className="mt-1">All prompts and responses are logged.</p>
        </footer>
      </div>
    );
  }

  return (
    <div className="flex w-full flex-col gap-4">
      <Hero />
      <Features />
      <HowItWorks />
      <CodeExample />
      <footer className="px-4 pb-6 text-center text-xs text-gray-500 sm:px-6 lg:px-8">
        <p>Service is provided without guarantee.</p>
        <p className="mt-1">All prompts and responses are logged.</p>
      </footer>
    </div>
  );
}
