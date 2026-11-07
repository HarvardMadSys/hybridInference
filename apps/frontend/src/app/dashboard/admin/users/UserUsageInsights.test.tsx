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

  const ANALYSIS = {
    analysis: 'Mostly coding-agent traffic.',
    model: 'glm-5.1',
    sampled_requests: 42,
    scope: 'user-1',
    generated_at: '2026-10-24T00:00:00Z',
  };

  it('says beside the buttons that the analysis is a paid, audited call', () => {
    render(<UserUsageInsights userId="user-1" />);
    expect(
      screen.getByText(/a paid call on the Usage Insights analysis key that is recorded/),
    ).toBeInTheDocument();
    expect(analyzeUsageInsights).not.toHaveBeenCalled();
  });

  it('runs the analysis once on the click, holding both buttons until the report shows', async () => {
    let finish!: (value: typeof ANALYSIS) => void;
    vi.mocked(analyzeUsageInsights).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve as (value: typeof ANALYSIS) => void;
        }) as ReturnType<typeof analyzeUsageInsights>,
    );
    render(<UserUsageInsights userId="user-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Analyze usage' }));

    // No confirmation: it runs at once, and nothing starts again while it runs.
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(analyzeUsageInsights).toHaveBeenCalledTimes(1);
    expect(analyzeUsageInsights).toHaveBeenCalledWith({ user_id: 'user-1', limit: 100 });
    expect(screen.getByRole('button', { name: 'Analyzing…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'View user turns' })).toBeDisabled();

    finish(ANALYSIS);
    expect(await screen.findByText('Mostly coding-agent traffic.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Re-analyze' })).toBeEnabled();
    expect(analyzeUsageInsights).toHaveBeenCalledTimes(1);
  });

  it('shows a failed analysis in the panel, and runs it again on the next click', async () => {
    vi.mocked(analyzeUsageInsights)
      .mockRejectedValueOnce(new Error('No analysis API key configured.'))
      .mockResolvedValueOnce(ANALYSIS);
    render(<UserUsageInsights userId="user-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Analyze usage' }));

    expect(await screen.findByText('No analysis API key configured.')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Analyze usage' }));
    expect(await screen.findByText('Mostly coding-agent traffic.')).toBeInTheDocument();
    expect(screen.queryByText('No analysis API key configured.')).toBeNull();
    expect(analyzeUsageInsights).toHaveBeenCalledTimes(2);
  });

  it('views user turns directly, without a confirmation', async () => {
    vi.mocked(sampleUsageInsights).mockResolvedValue({
      sampled_requests: 0,
      scope: 'user-1',
      samples: [],
    });
    render(<UserUsageInsights userId="user-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'View user turns' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(sampleUsageInsights).toHaveBeenCalledTimes(1));
  });
});
