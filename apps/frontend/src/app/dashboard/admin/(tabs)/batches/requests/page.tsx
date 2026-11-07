'use client';

import { useSearchParams } from 'next/navigation';

import { RequestsTab } from '@/components/features/admin/RequestsTab';

/**
 * The requests the batch worker ran, which Recent Requests leaves out.
 * `?batch=<id>` opens it on one batch (each batch's Requests link).
 */
export default function Page() {
  const batch = useSearchParams()?.get('batch') ?? '';
  return <RequestsTab source="batch" initialBatchId={batch} />;
}
