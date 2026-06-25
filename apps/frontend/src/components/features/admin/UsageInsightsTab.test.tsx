// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UsageInsightsTab } from './UsageInsightsTab';

vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock('@/lib/api/admin', () => ({
  analyzeUsageInsights: vi.fn(),
  getUsageInsightsSettings: vi.fn(),
}));

import { analyzeUsageInsights, getUsageInsightsSettings } from '@/lib/api/admin';

const mockAnalyze = vi.mocked(analyzeUsageInsights);
const mockGetSettings = vi.mocked(getUsageInsightsSettings);

function configured(c: boolean) {
  mockGetSettings.mockResolvedValue({
    configured: c,
    api_key_hint: c ? '…1234' : null,
    model: 'glm-5.1',
  });
}

describe('UsageInsightsTab', () => {
  beforeEach(() => {
    mockAnalyze.mockReset();
    mockGetSettings.mockReset();
    configured(true);
  });

  afterEach(() => {
    cleanup();
  });

  it('shows a configure-key notice when no key is set', async () => {
    configured(false);
    render(<UsageInsightsTab />);
    expect(await screen.findByText(/no analysis api key configured/i)).toBeInTheDocument();
  });

  it('analyzes (no key in the request) and renders the markdown analysis', async () => {
    mockAnalyze.mockResolvedValue({
      analysis: '## Client tools\n- Claude Code dominates',
      model: 'glm-5.1',
      sampled_requests: 12,
      scope: 'all users',
      generated_at: '2026-06-25T12:00:00Z',
    });

    render(<UsageInsightsTab />);
    fireEvent.click(screen.getByRole('button', { name: /analyze usage/i }));

    await waitFor(() => expect(mockAnalyze).toHaveBeenCalledTimes(1));
    // The key is server-side now; the request body carries no api_key.
    const arg = mockAnalyze.mock.calls[0][0];
    expect(arg).not.toHaveProperty('api_key');
    expect(await screen.findByText(/Claude Code dominates/i)).toBeInTheDocument();
  });

  it('falls back to the default sample size when the limit field is cleared', async () => {
    mockAnalyze.mockResolvedValue({
      analysis: 'ok',
      model: 'glm-5.1',
      sampled_requests: 1,
      scope: 'all users',
      generated_at: '2026-06-25T12:00:00Z',
    });

    render(<UsageInsightsTab />);
    // Clearing the field must not block the user; the request still gets a valid
    // limit (the default, 40) rather than 0/NaN.
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: /analyze usage/i }));

    await waitFor(() => expect(mockAnalyze).toHaveBeenCalledTimes(1));
    expect(mockAnalyze).toHaveBeenCalledWith(expect.objectContaining({ limit: 40 }));
  });

  it('scopes to a user email when provided', async () => {
    mockAnalyze.mockResolvedValue({
      analysis: 'ok',
      model: 'glm-5.1',
      sampled_requests: 1,
      scope: 'a@b.com',
      generated_at: '2026-06-25T12:00:00Z',
    });

    render(<UsageInsightsTab />);
    fireEvent.change(screen.getByPlaceholderText(/leave blank to analyze all users/i), {
      target: { value: 'a@b.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: /analyze usage/i }));

    await waitFor(() => expect(mockAnalyze).toHaveBeenCalledTimes(1));
    expect(mockAnalyze).toHaveBeenCalledWith(expect.objectContaining({ user_email: 'a@b.com' }));
  });

  it('surfaces API errors', async () => {
    mockAnalyze.mockRejectedValue(new Error('Analysis model returned an error'));

    render(<UsageInsightsTab />);
    fireEvent.click(screen.getByRole('button', { name: /analyze usage/i }));

    expect(await screen.findByText(/Analysis model returned an error/i)).toBeInTheDocument();
  });
});
