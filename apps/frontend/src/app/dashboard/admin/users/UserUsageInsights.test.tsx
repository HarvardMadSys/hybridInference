// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/admin', () => ({
  analyzeUsageInsights: vi.fn(),
  sampleUsageInsights: vi.fn(),
}));

import { analyzeUsageInsights, sampleUsageInsights } from '@/lib/api/admin';
import { UserUsageInsights } from './UserUsageInsights';

describe('UserUsageInsights', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('loads sampled user turns without calling the analysis model', async () => {
    vi.mocked(sampleUsageInsights).mockResolvedValue({
      sampled_requests: 1,
      scope: 'user-1',
      samples: [
        {
          timestamp: '2026-06-25T12:00:00Z',
          model_id: 'glm-5.1',
          provider: 'zhipu',
          user_agent: 'claude-cli/1.2.0',
          referer: null,
          system_opener: 'You are Claude Code.',
          user_messages: ['Fix the flaky test', 'Also update the changelog'],
        },
      ],
    });

    render(<UserUsageInsights userId="user-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'View user turns' }));

    await screen.findByText('Fix the flaky test');
    expect(screen.getByText('Also update the changelog')).toBeInTheDocument();
    expect(screen.getByText('You are Claude Code.')).toBeInTheDocument();
    expect(screen.getByText(/user turns only/)).toBeInTheDocument();
    expect(analyzeUsageInsights).not.toHaveBeenCalled();
    expect(sampleUsageInsights).toHaveBeenCalledWith({ user_id: 'user-1', limit: 100 });
  });

  it('does not send Analyze usage when viewing turns fails', async () => {
    vi.mocked(sampleUsageInsights).mockRejectedValue(new Error('No requests with stored payloads'));

    render(<UserUsageInsights userId="user-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'View user turns' }));

    await waitFor(() => {
      expect(screen.getByText('No requests with stored payloads')).toBeInTheDocument();
    });
    expect(analyzeUsageInsights).not.toHaveBeenCalled();
  });
});
