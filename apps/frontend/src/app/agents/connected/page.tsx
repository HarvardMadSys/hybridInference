'use client';

import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useState } from 'react';
import { connectGitHub } from '@/lib/api/agents';

// Where GitHub sends the browser back after the user authorizes the App.
//
// The browser only ever carries the code here; the platform is what exchanges
// it and asks GitHub which installations this user can reach. That is the
// whole point of the round trip — an installation id posted straight from a
// page would be a claim, not an entitlement.
function ConnectedInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const code = params?.get('code');
    if (!code) {
      setError('GitHub did not return an authorization code.');
      return;
    }
    connectGitHub(code)
      .then(() => router.replace('/agents'))
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : 'could not complete the connection'),
      );
  }, [params, router]);

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900">Could not connect GitHub</h1>
        <p className="max-w-md text-center text-sm text-gray-500">{error}</p>
        <Link href="/agents" className="text-sm font-medium text-crimson hover:underline">
          Back to Agents
        </Link>
      </div>
    );
  }

  return (
    <div className="flex h-full items-center justify-center">
      <p className="text-sm text-gray-500">Connecting GitHub…</p>
    </div>
  );
}

export default function AgentsConnectedPage() {
  return (
    <Suspense fallback={<div className="flex h-full items-center justify-center" />}>
      <ConnectedInner />
    </Suspense>
  );
}
