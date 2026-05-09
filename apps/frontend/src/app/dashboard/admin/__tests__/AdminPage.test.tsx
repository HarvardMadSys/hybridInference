// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AdminLayout from '../layout';
import AdminPage from '../page';

const replace = vi.fn();
let pathname = '/dashboard/admin';
let usersTabMounts = 0;
let authState = {
  loading: false,
  isAuthenticated: true,
  user: { is_admin: true },
};

vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: { href: string; children: ReactNode }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
  useRouter: () => ({ replace }),
}));

vi.mock('@/components/features/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: authState,
  }),
}));

vi.mock('@/lib/api/admin', () => ({
  listAuditLog: vi.fn(),
  listRecentRequests: vi.fn(),
  getRequestMetrics: vi.fn(),
  previewBroadcast: vi.fn(),
  sendTestBroadcastEmail: vi.fn(),
  createBroadcast: vi.fn(),
  listBroadcasts: vi.fn(),
  getBroadcastDetail: vi.fn(),
  cancelBroadcast: vi.fn(),
  exportRequests: vi.fn(),
  getProviderQuotas: vi.fn(),
  getRecentRequestContent: vi.fn(),
}));

vi.mock('../AnalyticsTab', () => ({
  AnalyticsTab: () => <div>analytics tab</div>,
}));

vi.mock('../ProviderKeysTab', () => ({
  ProviderKeysTab: () => <div>provider keys tab</div>,
}));

vi.mock('../ProviderPerformanceTab', () => ({
  ProviderPerformanceTab: () => <div>provider performance tab</div>,
}));

vi.mock('../SettingsTab', () => ({
  SettingsTab: () => <div>settings tab</div>,
}));

vi.mock('../TokenUsageTab', () => ({
  TokenUsageTab: () => <div>usage tab</div>,
}));

vi.mock('../users', () => ({
  default: function MockUsersTab() {
    const [mountId] = useState(() => {
      usersTabMounts += 1;
      return usersTabMounts;
    });
    return <div>{`users tab content ${mountId}`}</div>;
  },
}));

vi.mock('../AdminRecentRequestDetailPanel', () => ({
  AdminRecentRequestDetailPanel: () => <div>request detail panel</div>,
}));

describe('AdminPage', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    replace.mockClear();
    pathname = '/dashboard/admin';
    usersTabMounts = 0;
    authState = {
      loading: false,
      isAuthenticated: true,
      user: { is_admin: true },
    };
    window.history.replaceState({}, '', '/dashboard/admin');
  });

  it('renders landing page content without duplicating the shared admin shell', () => {
    render(
      <AdminLayout>
        <AdminPage />
      </AdminLayout>,
    );

    expect(screen.getAllByRole('heading', { name: 'Admin' })).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: 'Dashboard' })).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'Dashboard' })).toHaveAttribute('href', '/dashboard');
    expect(screen.getByRole('link', { name: 'Users' })).toHaveAttribute(
      'href',
      '/dashboard/admin/users',
    );
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'href',
      '/dashboard/admin/settings',
    );
    expect(screen.getByRole('link', { name: 'Users' })).toHaveClass('bg-gray-900', 'text-white');
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeVisible();
    expect(screen.getByText('users tab content 1')).toBeVisible();
    expect(replace).not.toHaveBeenCalled();
  });

  it('ignores legacy tab query params on the root admin route', () => {
    window.history.replaceState({}, '', '/dashboard/admin?tab=requests');

    render(
      <AdminLayout>
        <AdminPage />
      </AdminLayout>,
    );

    expect(screen.getByText('users tab content 1')).toBeVisible();
    expect(screen.queryByText('request detail panel')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeVisible();
  });

  it('refreshes the landing users view without triggering legacy requests APIs', async () => {
    const adminApi = await import('@/lib/api/admin');

    render(
      <AdminLayout>
        <AdminPage />
      </AdminLayout>,
    );

    expect(screen.getByText('users tab content 1')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(screen.getByText('users tab content 2')).toBeVisible();
    expect(adminApi.listRecentRequests).not.toHaveBeenCalled();
    expect(adminApi.getRequestMetrics).not.toHaveBeenCalled();
  });
});
