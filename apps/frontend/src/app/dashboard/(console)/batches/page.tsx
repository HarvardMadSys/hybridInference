'use client';

import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { BatchesView } from '@/components/features/batches/BatchesView';

export default function BatchesPage() {
  return (
    <ProtectedRoute>
      <BatchesView />
    </ProtectedRoute>
  );
}
