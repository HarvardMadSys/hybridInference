'use client';

import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { DashboardView } from '@/components/features/dashboard/DashboardView';

export default function DashboardPage() {
  return (
    <ProtectedRoute>
      <DashboardView />
    </ProtectedRoute>
  );
}
