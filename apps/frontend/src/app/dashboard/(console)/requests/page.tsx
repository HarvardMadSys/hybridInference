'use client';

import { RecentRequests } from '@/components/features/dashboard/RecentRequests';
import { useT } from '@/components/providers/useT';

export default function RequestsPage() {
  const t = useT();
  return (
    <>
      <h1 className="sr-only">{t('console.nav.requests', 'Requests')}</h1>
      <RecentRequests />
    </>
  );
}
