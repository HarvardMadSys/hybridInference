'use client';

import { useSearchParams } from 'next/navigation';
import { Suspense } from 'react';

import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { CreateBatchView } from '@/components/features/batches/CreateBatchView';

function CreateBatchRoute() {
  const params = useSearchParams();
  return <CreateBatchView copyFrom={params.get('copy')} />;
}

export default function NewBatchPage() {
  return (
    <ProtectedRoute>
      <Suspense fallback={null}>
        <CreateBatchRoute />
      </Suspense>
    </ProtectedRoute>
  );
}
