'use client';

import { useState } from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import UsersTab from './users';

export default function AdminPage() {
  const [usersRefreshKey, setUsersRefreshKey] = useState(0);

  return (
    <ProtectedRoute>
      <div className="mx-auto w-full max-w-4xl pb-20">
        <div className="mb-10 flex justify-end">
          <button
            type="button"
            onClick={() => setUsersRefreshKey((key) => key + 1)}
            className="text-[13px] text-gray-400 transition hover:text-gray-900"
          >
            Refresh
          </button>
        </div>

        <UsersTab key={usersRefreshKey} />
      </div>
    </ProtectedRoute>
  );
}
