'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { config } from '@/config/env';
import { useAuth } from '@/components/providers';

export function Header() {
  const router = useRouter();
  const { state, logout } = useAuth();

  const handleLogout = async () => {
    await logout();
    router.replace('/');
  };

  return (
    <header className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-6">
      <div className="flex items-center gap-3">
        <Link
          href="/"
          aria-label={`${config.appName} home`}
          className="flex items-center gap-2 text-xl font-bold tracking-tight"
        >
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-crimson font-serif text-sm font-bold text-white shadow-sm">
            FI
          </span>
          <span>{config.appName}</span>
        </Link>
        <a
          href="https://madsys.seas.harvard.edu"
          className="font-serif text-sm text-gray-500 hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          Harvard SEAS
        </a>
      </div>
      <div className="flex items-center gap-2">
        <a
          href="https://status.freeinference.org/"
          className="rounded-md px-3 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900"
          target="_blank"
          rel="noopener noreferrer"
        >
          Status
        </a>
        {state.isAuthenticated && (
          <>
            <Link
              href="/dashboard"
              className="rounded-md px-3 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900"
            >
              Dashboard
            </Link>
            <button
              type="button"
              onClick={handleLogout}
              className="rounded-md px-3 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900"
            >
              Log out
            </button>
          </>
        )}
      </div>
    </header>
  );
}
