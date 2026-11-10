'use client';

import { useSearchParams } from 'next/navigation';

import { RequestsTab } from '@/components/features/admin/RequestsTab';

/**
 * The requests the batch worker ran, which Recent Requests leaves out.
 * `?batch=<id>` opens it on one batch (each batch's Requests link);
 * `?from=<instant>&to=<instant>` on one stretch (a Request volume bar's).
 */
export default function Page() {
  const params = useSearchParams();
  return (
    <RequestsTab
      source="batch"
      initialBatchId={params?.get('batch') ?? ''}
      initialWindowStart={params?.get('from') ?? ''}
      initialWindowEnd={params?.get('to') ?? ''}
    />
  );
}
