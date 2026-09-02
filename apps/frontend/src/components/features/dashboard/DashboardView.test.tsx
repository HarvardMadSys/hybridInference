// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let agentsEnabled = false;

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      user: {
        id: 'internal-user',
        email: 'internal@example.test',
        user_name: 'Internal User',
        role: 'internal',
      },
    },
  }),
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({
    branding: { docsUrl: '' },
    features: { agents: agentsEnabled },
  }),
}));

vi.mock('@/components/features/dashboard/ApiKeyManager', () => ({ ApiKeyManager: () => null }));
vi.mock('@/components/features/dashboard/ModelsSection', () => ({ ModelsSection: () => null }));
vi.mock('@/components/features/dashboard/RecentRequests', () => ({ RecentRequests: () => null }));
vi.mock('@/components/features/dashboard/UsageStats', () => ({ UsageStats: () => null }));
vi.mock('@/components/ui/UpdatesBanner', () => ({ UpdatesBanner: () => null }));

import { DashboardView } from './DashboardView';

describe('DashboardView runtime agents gate', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    agentsEnabled = false;
  });

  it('hides the Agents link when the server-only proxy pair is unavailable', () => {
    render(<DashboardView />);

    expect(screen.queryByRole('link', { name: 'Agents' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /api playground/i })).toBeInTheDocument();
  });

  it('shows the Agents link when the runtime feature is enabled', () => {
    agentsEnabled = true;

    render(<DashboardView />);

    expect(screen.getByRole('link', { name: 'Agents' })).toHaveAttribute('href', '/agents');
  });
});
