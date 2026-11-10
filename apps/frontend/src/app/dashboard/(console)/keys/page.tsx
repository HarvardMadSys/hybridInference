'use client';

import { ApiKeyManager } from '@/components/features/dashboard/ApiKeyManager';
import { useT } from '@/components/providers/useT';

export default function KeysPage() {
  const t = useT();
  return (
    <>
      <h1 className="sr-only">{t('console.nav.keys', 'API Keys')}</h1>
      <ApiKeyManager />
    </>
  );
}
