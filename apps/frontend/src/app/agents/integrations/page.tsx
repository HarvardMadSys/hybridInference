'use client';

import { Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { IntegrationsView } from '@/components/features/agents/IntegrationsView';
import type { AgentIntegrationProvider } from '@/lib/api/agents';

function IntegrationsPageContent() {
  const params = useSearchParams();
  const connected = params?.get('connected');
  const connectedProvider: AgentIntegrationProvider | null =
    connected === 'github' || connected === 'gitlab' ? connected : null;

  return <IntegrationsView connectedProvider={connectedProvider} />;
}

export default function IntegrationsPage() {
  return (
    <Suspense fallback={<div className="h-full bg-white" />}>
      <IntegrationsPageContent />
    </Suspense>
  );
}
