'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { AgentsSidebar } from '@/components/features/agents/AgentsSidebar';
import { useAuth } from '@/components/providers';
import { hasRole } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';

// App-mode shell (same fixed-overlay pattern as the playground): slim header +
// repo/task sidebar + main pane. Deep links keep working — /agents/* are real
// routes; only the marketing chrome is replaced.
function AgentsHeader() {
  const router = useRouter();
  const { logout } = useAuth();
  const { branding, features } = useSiteConfig();

  const handleLogout = async () => {
    await logout();
    router.replace('/');
  };

  return (
    <header className="flex shrink-0 items-center justify-between border-b border-gray-200 px-5 py-2.5">
      <div className="flex min-w-0 items-baseline gap-2">
        <Link href="/" className="text-lg font-bold tracking-tight">
          {branding.appName}
        </Link>
        {branding.orgName && (
          <a
            href={branding.orgUrl}
            className="font-serif text-[13px] text-gray-500 hover:text-crimson"
            target="_blank"
            rel="noopener noreferrer"
          >
            {branding.orgName}
          </a>
        )}
      </div>
      <div className="flex items-center gap-1 text-[13px] font-medium text-gray-600">
        <a
          href={branding.statusUrl}
          className="rounded-md px-2.5 py-1 hover:bg-gray-100 hover:text-gray-900"
          target="_blank"
          rel="noopener noreferrer"
        >
          Status
        </a>
        {features.rag && (
          <Link
            href="/chat"
            className="rounded-md px-2.5 py-1 hover:bg-gray-100 hover:text-gray-900"
          >
            Docs Assistant
          </Link>
        )}
        <span className="rounded-md bg-gray-100 px-2.5 py-1 text-gray-900">Agents</span>
        <Link
          href="/dashboard"
          className="rounded-md px-2.5 py-1 hover:bg-gray-100 hover:text-gray-900"
        >
          Dashboard
        </Link>
        <button
          type="button"
          onClick={handleLogout}
          className="rounded-md px-2.5 py-1 hover:bg-gray-100 hover:text-gray-900"
        >
          Log out
        </button>
      </div>
    </header>
  );
}

function AgentsShell({ children }: { children: React.ReactNode }) {
  const { state } = useAuth();

  // Internal-only while the sandbox is dogfood (P0) — same gate as the
  // playground; opens up behind a site-config feature flag in P1.
  if (!hasRole(state.user?.role, 'internal')) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900">Internal access required</h1>
        <p className="text-sm text-gray-500">
          The agent sandbox is dogfood-only for now (issue #1041).
        </p>
        <Link href="/dashboard" className="text-sm font-medium text-crimson hover:underline">
          Back to Dashboard
        </Link>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1">
      <AgentsSidebar />
      <main className="min-w-0 flex-1 overflow-y-auto bg-white">{children}</main>
    </div>
  );
}

export default function AgentsLayout({ children }: { children: React.ReactNode }) {
  return (
    <ProtectedRoute>
      <div className="fixed inset-0 z-50 flex flex-col bg-white">
        <AgentsHeader />
        <AgentsShell>{children}</AgentsShell>
      </div>
    </ProtectedRoute>
  );
}
