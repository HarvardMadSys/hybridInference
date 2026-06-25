// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UsageInsightsTab } from './UsageInsightsTab';

vi.mock('@/lib/api/admin', () => ({
  analyzeUsageInsights: vi.fn(),
}));

import { analyzeUsageInsights } from '@/lib/api/admin';

const mockAnalyze = vi.mocked(analyzeUsageInsights);

describe('UsageInsightsTab', () => {
  beforeEach(() => {
    window.localStorage.clear();
    mockAnalyze.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it('requires an API key before analyzing', async () => {
    render(<UsageInsightsTab />);
    fireEvent.click(screen.getByRole('button', { name: /analyze usage/i }));

    expect(await screen.findByText(/enter a freeinference\.org api key/i)).toBeInTheDocument();
    expect(mockAnalyze).not.toHaveBeenCalled();
  });

  it('calls the API and renders the markdown analysis', async () => {
    mockAnalyze.mockResolvedValue({
      analysis: '## Client tools\n- Claude Code dominates',
      model: 'glm-5.2',
      sampled_requests: 12,
      scope: 'all users',
      generated_at: '2026-06-25T12:00:00Z',
    });

    render(<UsageInsightsTab />);
    fireEvent.change(screen.getByPlaceholderText('sk-...'), {
      target: { value: 'sk-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: /analyze usage/i }));

    await waitFor(() => expect(mockAnalyze).toHaveBeenCalledTimes(1));
    expect(mockAnalyze).toHaveBeenCalledWith(
      expect.objectContaining({ api_key: 'sk-secret', model: 'glm-5.2' }),
    );
    expect(await screen.findByText(/Claude Code dominates/i)).toBeInTheDocument();
    // The key is remembered in localStorage for next visit.
    expect(window.localStorage.getItem('usage_insights_api_key')).toBe('sk-secret');
  });

  it('surfaces API errors', async () => {
    mockAnalyze.mockRejectedValue(new Error('Analysis model returned an error'));

    render(<UsageInsightsTab />);
    fireEvent.change(screen.getByPlaceholderText('sk-...'), {
      target: { value: 'sk-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: /analyze usage/i }));

    expect(await screen.findByText(/Analysis model returned an error/i)).toBeInTheDocument();
  });
});
