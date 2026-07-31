'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { AgentsSidebar } from '@/components/features/agents/AgentsSidebar';
import { useAuth } from '@/components/providers';
import { hasRole } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';

export const SIDEBAR_COLLAPSED_STORAGE_KEY = 'agents.sidebar.collapsed';

// App-mode shell (same fixed-overlay pattern as the playground): slim header +
// repo/task sidebar + main pane. Deep links keep working — /agents/* are real
// routes; only the marketing chrome is replaced.
function AgentsHeader({
  sidebarCollapsed,
  onToggleSidebar,
}: {
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
}) {
  const router = useRouter();
  const { logout } = useAuth();
  const { branding, features } = useSiteConfig();

  const handleLogout = async () => {
    await logout();
    router.replace('/');
  };

  return (
    <header className="flex shrink-0 items-center justify-between border-b border-gray-200/80 bg-white/95 px-5 py-2.5 shadow-[0_1px_0_rgba(17,24,39,0.02)] backdrop-blur">
      <div className="flex min-w-0 items-baseline gap-2">
        <button
          type="button"
          onClick={onToggleSidebar}
          aria-label={sidebarCollapsed ? 'Expand task list' : 'Collapse task list'}
          aria-expanded={!sidebarCollapsed}
          aria-controls="agents-sidebar"
          title={sidebarCollapsed ? 'Expand task list' : 'Collapse task list'}
          className="-ml-1.5 self-center rounded-md p-1.5 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/25"
        >
          <svg
            aria-hidden="true"
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.8}
          >
            <rect x="3.5" y="4" width="17" height="16" rx="2" />
            <path d="M9.5 4v16" />
          </svg>
        </button>
        <Link
          href="/"
          className="text-[17px] font-semibold tracking-[-0.02em] text-gray-950 transition-colors hover:text-crimson"
        >
          {branding.appName}
        </Link>
        {branding.orgName && (
          <a
            href={branding.orgUrl}
            className="font-serif text-[13px] tracking-[0.01em] text-gray-500 transition-colors hover:text-crimson"
            target="_blank"
            rel="noopener noreferrer"
          >
            {branding.orgName}
          </a>
        )}
      </div>
      <nav
        aria-label="Product navigation"
        className="flex items-center gap-0.5 text-[13px] font-medium text-gray-600"
      >
        <a
          href={branding.statusUrl}
          className="rounded-md px-2.5 py-1.5 transition-colors hover:bg-gray-100 hover:text-gray-900"
          target="_blank"
          rel="noopener noreferrer"
        >
          Status
        </a>
        {features.rag && (
          <Link
            href="/chat"
            className="rounded-md px-2.5 py-1.5 transition-colors hover:bg-gray-100 hover:text-gray-900"
          >
            Docs Assistant
          </Link>
        )}
        <span className="rounded-md bg-crimson/[0.06] px-2.5 py-1.5 text-crimson ring-1 ring-inset ring-crimson/10">
          Agents
        </span>
        <Link
          href="/dashboard"
          className="rounded-md px-2.5 py-1.5 transition-colors hover:bg-gray-100 hover:text-gray-900"
        >
          Dashboard
        </Link>
        <button
          type="button"
          onClick={handleLogout}
          className="rounded-md px-2.5 py-1.5 transition-colors hover:bg-gray-100 hover:text-gray-900"
        >
          Log out
        </button>
      </nav>
    </header>
  );
}

function AgentsShell({
  children,
  sidebarCollapsed,
}: {
  children: React.ReactNode;
  sidebarCollapsed: boolean;
}) {
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
      <AgentsSidebar collapsed={sidebarCollapsed} />
      <main className="min-w-0 flex-1 overflow-y-auto bg-gray-50/40">{children}</main>
    </div>
  );
}

export default function AgentsLayout({ children }: { children: React.ReactNode }) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  // Read after mount so the server-rendered shell and the first client render
  // agree; the sidebar then folds away on the same frame as hydration.
  useEffect(() => {
    try {
      setSidebarCollapsed(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === '1');
    } catch {
      // Blocked storage just means the sidebar starts open.
    }
  }, []);

  function toggleSidebar() {
    setSidebarCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, next ? '1' : '0');
      } catch {
        // Non-persisted collapse still works for this session.
      }
      return next;
    });
  }

  return (
    <ProtectedRoute>
      <div className="fixed inset-0 z-50 flex flex-col bg-gray-50">
        <AgentsHeader sidebarCollapsed={sidebarCollapsed} onToggleSidebar={toggleSidebar} />
        <AgentsShell sidebarCollapsed={sidebarCollapsed}>{children}</AgentsShell>
      </div>
    </ProtectedRoute>
  );
}
