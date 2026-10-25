// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { getUserFilterProviders } from '@/lib/api/admin';

import { chooseOption, optionLabels } from '@/test-utils/select';

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

    const trigger = screen.getByRole('combobox', { name: 'Filter by provider' });
    expect(trigger).toHaveTextContent('Any provider');
    fireEvent.click(trigger);
    await waitFor(() =>
      expect(optionLabels()).toEqual([
        'Any provider',
        'OpenRouter',
        'Retired (no longer routed)',
        'vLLM',
      ]),
    );
  });

  it('reports a chosen provider, and null for Any provider', async () => {
    vi.mocked(getUserFilterProviders).mockResolvedValue({
      providers: [
        { provider: 'openrouter', display_name: 'OpenRouter', in_logs: true, routable: true },
      ],
    });
    const onChange = vi.fn();

    const { rerender } = render(<ProviderFilter value={null} onChange={onChange} />);
    await act(async () => {});

    chooseOption('Filter by provider', 'OpenRouter');
    expect(onChange).toHaveBeenLastCalledWith('openrouter');

    rerender(<ProviderFilter value="openrouter" onChange={onChange} />);
    expect(screen.getByRole('combobox', { name: 'Filter by provider' })).toHaveTextContent(
      'OpenRouter',
    );
    chooseOption('Filter by provider', 'Any provider');
    expect(onChange).toHaveBeenLastCalledWith(null);
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it('keeps the current selection selectable when the list cannot be loaded', async () => {
    vi.mocked(getUserFilterProviders).mockRejectedValue(new Error('offline'));

    render(<ProviderFilter value="chutes" onChange={vi.fn()} />);
    await act(async () => {});

    const trigger = screen.getByRole('combobox', { name: 'Filter by provider' });
    expect(trigger).toHaveTextContent('chutes');
    fireEvent.click(trigger);
    expect(
      within(screen.getByRole('listbox')).getByRole('option', { name: 'chutes' }),
    ).toHaveAttribute('aria-selected', 'true');
  });
});
