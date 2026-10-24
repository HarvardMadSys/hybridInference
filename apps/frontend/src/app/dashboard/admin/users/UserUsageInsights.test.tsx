// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

  it('asks before the paid analysis, and Cancel sends nothing', async () => {
    render(<UserUsageInsights userId="user-1" userLabel="user@example.com" />);
    fireEvent.click(screen.getByRole('button', { name: 'Analyze usage' }));

    const dialog = screen.getByRole('dialog', { name: 'Analyze usage for user@example.com?' });
    expect(dialog).toHaveTextContent('up to 100 of user@example.com');
    expect(dialog).toHaveTextContent('counts toward its usage and cost');
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(analyzeUsageInsights).not.toHaveBeenCalled();
  });

  it('runs the analysis once on Confirm and shows the report', async () => {
    let finish!: (value: typeof ANALYSIS) => void;
    vi.mocked(analyzeUsageInsights).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve as (value: typeof ANALYSIS) => void;
        }) as ReturnType<typeof analyzeUsageInsights>,
    );
    render(<UserUsageInsights userId="user-1" userLabel="user@example.com" />);
    fireEvent.click(screen.getByRole('button', { name: 'Analyze usage' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Analyze' }));

    // Busy: no second run, no closing.
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: 'Analyzing…' })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Analyzing…' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(analyzeUsageInsights).toHaveBeenCalledTimes(1);

    finish(ANALYSIS);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(analyzeUsageInsights).toHaveBeenCalledWith({ user_id: 'user-1', limit: 100 });
    expect(await screen.findByText('Mostly coding-agent traffic.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Re-analyze' })).toBeInTheDocument();
  });

  it('keeps the confirmation open with the error when the analysis fails, and retries', async () => {
    vi.mocked(analyzeUsageInsights)
      .mockRejectedValueOnce(new Error('No analysis API key configured.'))
      .mockResolvedValueOnce(ANALYSIS);
    render(<UserUsageInsights userId="user-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Analyze usage' }));
    const dialog = screen.getByRole('dialog', { name: 'Analyze usage for user-1?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Analyze' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'No analysis API key configured.',
    );
    fireEvent.click(within(dialog).getByRole('button', { name: 'Analyze' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
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
