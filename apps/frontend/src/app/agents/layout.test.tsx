// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

vi.mock('@/components/features/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: { user: { user_name: 'Ada', email: 'ada@example.com', role: 'internal' } },
    logout: vi.fn(),
  }),
}));

vi.mock('@/components/providers/AuthProvider', () => ({
  hasRole: () => true,
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({
    branding: { appName: 'FreeInference', orgName: '', orgUrl: '', statusUrl: '/status' },
    features: { rag: false },
  }),
}));

vi.mock('@/components/features/agents/AgentsSidebar', () => ({
  AgentsSidebar: ({ collapsed }: { collapsed?: boolean }) =>
    collapsed ? null : <aside data-testid="sidebar-stub">tasks</aside>,
}));

import AgentsLayout, { SIDEBAR_COLLAPSED_STORAGE_KEY } from './layout';

function toggle(): HTMLElement {
  return screen.getByRole('button', { name: /(Collapse|Expand) task list/ });
}

describe('AgentsLayout sidebar collapse', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => cleanup());

  it('collapses and restores the task list from the header toggle', () => {
    render(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );

    expect(screen.getByTestId('sidebar-stub')).toBeInTheDocument();
    expect(toggle()).toHaveAccessibleName('Collapse task list');
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
    expect(toggle()).toHaveAttribute('aria-controls', 'agents-sidebar');

    fireEvent.click(toggle());

    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(toggle()).toHaveAccessibleName('Expand task list');
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('1');

    fireEvent.click(toggle());

    expect(screen.getByTestId('sidebar-stub')).toBeInTheDocument();
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('0');
  });

  it('starts collapsed when that is how it was left', () => {
    localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, '1');

    render(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );

    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(toggle()).toHaveAccessibleName('Expand task list');
  });
});
