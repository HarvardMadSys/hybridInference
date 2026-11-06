'use client';

import { ModelsSection } from '@/components/features/dashboard/ModelsSection';
import { useT } from '@/components/providers/useT';

export default function ModelsPage() {
  const t = useT();
  return (
    <>
      <h1 className="sr-only">{t('console.nav.models', 'Models')}</h1>
      <ModelsSection />
    </>
  );
}
