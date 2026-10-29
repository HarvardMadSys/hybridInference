'use client';

import { useParams } from 'next/navigation';

import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { BatchDetailView } from '@/components/features/batches/BatchDetailView';

export default function BatchDetailPage() {
  const params = useParams<{ id: string }>();
  return (
    <ProtectedRoute>
      <BatchDetailView batchId={decodeURIComponent(params.id)} />
    </ProtectedRoute>
  );
}
