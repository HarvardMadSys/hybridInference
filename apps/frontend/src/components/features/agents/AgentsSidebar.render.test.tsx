// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/navigation', () => ({
  usePathname: () => '/agents',
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { user_name: 'Ada', email: 'ada@example.com' } } }),
}));

vi.mock('@/lib/api/agents', () => ({
  archiveAgentJob: vi.fn(),
  getAgentJob: vi.fn(),
  getAgentJobArtifact: vi.fn(),
  getAgentJobFiles: vi.fn(),
  getAgentJobThread: vi.fn(),
  listAgentJobEvents: vi.fn(),
  listAgentJobs: vi.fn(async () => []),
  streamAgentJob: vi.fn(),
}));

import { AgentsSidebar, SIDEBAR_WIDTH_STORAGE_KEY } from './AgentsSidebar';

describe('AgentsSidebar layout', () => {
  beforeEach(() => {
    localStorage.clear();
    window.innerWidth = 1400;
  });

  afterEach(() => cleanup());

  it('renders at the remembered width and resizes from the seam', async () => {
    render(<AgentsSidebar />);
    await waitFor(() => expect(screen.getByText('No jobs yet.')).toBeInTheDocument());

    const sidebar = document.getElementById('agents-sidebar');
    expect(sidebar).toHaveStyle({ width: '288px' });

    const seam = screen.getByRole('separator', { name: 'Resize task list' });
    expect(seam).toHaveAttribute('aria-controls', 'agents-sidebar');
    fireEvent.pointerDown(seam, { button: 0, clientX: 288 });
    fireEvent.pointerMove(window, { clientX: 360 });
    fireEvent.pointerUp(window, { clientX: 360 });

    expect(sidebar).toHaveStyle({ width: '360px' });
    expect(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)).toBe('360');
  });

  it('renders neither the list nor its seam when collapsed', () => {
    render(<AgentsSidebar collapsed />);

    expect(document.getElementById('agents-sidebar')).not.toBeInTheDocument();
    expect(screen.queryByRole('separator', { name: 'Resize task list' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'New task' })).not.toBeInTheDocument();
  });
});
