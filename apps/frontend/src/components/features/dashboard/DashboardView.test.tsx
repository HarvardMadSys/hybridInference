// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

// The Agents entry point is a link into a *separate* service, proxied under
// `/agents` only when a deployment runs one. The role check says who may use
// it; whether it is there at all is a different question, and this is the
// test that keeps the two apart. `next.config.agents.test.ts` pins that the
// value is derived from the rewrite's own condition; this pins that the
// component honours it.

vi.mock('@/components/features/dashboard/ApiKeyManager', () => ({ ApiKeyManager: () => null }));
vi.mock('@/components/features/dashboard/ModelsSection', () => ({ ModelsSection: () => null }));
vi.mock('@/components/features/dashboard/RecentRequests', () => ({ RecentRequests: () => null }));
vi.mock('@/components/features/dashboard/UsageStats', () => ({ UsageStats: () => null }));
vi.mock('@/components/ui/UpdatesBanner', () => ({ UpdatesBanner: () => null }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { role: 'internal' } } }),
}));
vi.mock('@/components/providers/AuthProvider', () => ({
  hasRole: (userRole: string | undefined, required: string) => userRole === required,
}));

// `config` reads the environment at module load, so each case sets the
// variable and re-imports. `vi.resetModules()` is what makes the re-import
// real rather than a cache hit.
async function renderWith(agentsEnabled: string | undefined) {
  if (agentsEnabled === undefined) delete process.env.NEXT_PUBLIC_AGENTS_ENABLED;
  else process.env.NEXT_PUBLIC_AGENTS_ENABLED = agentsEnabled;
  vi.resetModules();
  const { DashboardView } = await import('./DashboardView');
  return render(<DashboardView />);
}

describe('the Agents entry point on the dashboard', () => {
  const saved = process.env.NEXT_PUBLIC_AGENTS_ENABLED;

  beforeEach(() => vi.resetModules());
  afterEach(() => {
    if (saved === undefined) delete process.env.NEXT_PUBLIC_AGENTS_ENABLED;
    else process.env.NEXT_PUBLIC_AGENTS_ENABLED = saved;
    cleanup();
  });

  it('is offered where a cloud agent is deployed', async () => {
    await renderWith('true');

    const link = screen.getByRole('link', { name: /agents/i });
    expect(link).toHaveAttribute('href', '/agents');
  });

  it('is absent where none is, while the rest of Internal Tools stays', async () => {
    // The gate is on the one link, not on the section: an internal user on a
    // deployment without an agent still gets the tools that are actually there.
    await renderWith(undefined);

    expect(screen.queryByRole('link', { name: /agents/i })).not.toBeInTheDocument();
    expect(screen.getByText('Internal Tools')).toBeInTheDocument();
  });
});
