// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { getUserFilterProviders } from '@/lib/api/admin';

import { ProviderFilter } from './ProviderFilter';

vi.mock('@/lib/api/admin', () => ({
  getUserFilterProviders: vi.fn(),
}));

afterEach(cleanup);

describe('ProviderFilter', () => {
  it('offers every provider the filter window can match, marking ones no longer routed', async () => {
    vi.mocked(getUserFilterProviders).mockResolvedValue({
      providers: [
        { provider: 'openrouter', display_name: 'OpenRouter', in_logs: true, routable: true },
        { provider: 'retired-vendor', display_name: 'Retired', in_logs: true, routable: false },
        { provider: 'vllm', display_name: 'vLLM', in_logs: false, routable: true },
      ],
    });

    render(<ProviderFilter value={null} onChange={vi.fn()} />);

    const select = screen.getByRole('combobox');
    expect(await within(select).findByRole('option', { name: 'OpenRouter' })).toBeInTheDocument();
    expect(within(select).getByRole('option', { name: 'Retired (no longer routed)' })).toHaveValue(
      'retired-vendor',
    );
    expect(within(select).getByRole('option', { name: 'vLLM' })).toBeInTheDocument();
    expect(within(select).getByRole('option', { name: 'Any provider' })).toHaveValue('');
  });

  it('keeps the current selection selectable when the list cannot be loaded', async () => {
    vi.mocked(getUserFilterProviders).mockRejectedValue(new Error('offline'));

    render(<ProviderFilter value="chutes" onChange={vi.fn()} />);

    const select = screen.getByRole('combobox');
    expect(select).toHaveValue('chutes');
    expect(within(select).getByRole('option', { name: 'chutes' })).toBeInTheDocument();
  });
});
