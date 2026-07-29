'use client';

import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';
import { connectGitHub, connectGitLab } from '@/lib/api/agents';
import type { AgentIntegrationProvider } from '@/lib/api/agents';

function ConnectedInner() {
  const router = useRouter();
  const params = useSearchParams();
  const connectStarted = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const providerParam = params?.get('provider');
  const provider: AgentIntegrationProvider = providerParam === 'gitlab' ? 'gitlab' : 'github';
  const providerName = provider === 'gitlab' ? 'GitLab' : 'GitHub';
  const code = params?.get('code');
  const state = params?.get('state');
  const oauthError = params?.get('error_description') ?? params?.get('error');

  useEffect(() => {
    // React intentionally replays effects in development Strict Mode. OAuth
    // state is single-use, so submitting twice would turn a successful first
    // request into a misleading replay error from the second one.
    if (connectStarted.current) return;
    connectStarted.current = true;

    if (oauthError) {
      setError(oauthError);
      return;
    }
    if (!code) {
      setError(`${providerName} did not return an authorization code.`);
      return;
    }
    if (!state) {
      setError(`${providerName} did not return the connection state.`);
      return;
    }

    const connect = provider === 'gitlab' ? connectGitLab : connectGitHub;
    connect(code, state)
      .then(() => router.replace(`/agents/integrations?connected=${provider}`))
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : 'could not complete the connection'),
      );
  }, [code, oauthError, provider, providerName, router, state]);

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900">Could not connect {providerName}</h1>
        <p className="max-w-md text-center text-sm text-gray-500">{error}</p>
        <Link
          href="/agents/integrations"
          className="text-sm font-medium text-crimson hover:underline"
        >
          Back to integrations
        </Link>
      </div>
    );
  }

  return (
    <div className="flex h-full items-center justify-center">
      <p className="text-sm text-gray-500">Connecting {providerName}…</p>
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
