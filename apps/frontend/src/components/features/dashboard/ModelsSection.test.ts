// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { createElement } from 'react';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ModelCatalogItem } from '@/lib/api/user';
import { useModels } from '@/lib/hooks';
import { isDashboardModelVisible, ModelsSection } from './ModelsSection';

vi.mock('@/lib/hooks', () => ({ useModels: vi.fn() }));

function makeModel(overrides: Partial<ModelCatalogItem> = {}): ModelCatalogItem {
  return {
    id: 'test-model',
    name: 'Test Model',
    object: 'model',
    created: 0,
    owned_by: 'mistral',
    input_modalities: [],
    output_modalities: [],
    quantization: '',
    context_length: 0,
    max_output_length: 0,
    pricing: {},
    supported_sampling_parameters: [],
    supported_features: [],
    ...overrides,
  };
}

describe('isDashboardModelVisible', () => {
  it('shows an OpenAI-owned model returned by the user models API to free users', () => {
    expect(isDashboardModelVisible(makeModel({ id: 'gpt-5.3-spark', owned_by: 'openai' }))).toBe(
      true,
    );
  });

  it('shows Anthropic-owned models returned by the user models API', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'Anthropic' }))).toBe(true);
  });

  it('shows OpenAI-owned models returned by the user models API', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'OpenAI' }))).toBe(true);
  });

  it('shows unrelated third-party models returned by the user models API', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'mistral' }))).toBe(true);
  });
});

describe('ModelsSection pricing', () => {
  afterEach(() => cleanup());

  function renderPricing(pricing: Record<string, string>) {
    vi.mocked(useModels).mockReturnValue({
      data: { data: [makeModel({ pricing })] },
      isLoading: false,
      error: null,
    } as ReturnType<typeof useModels>);
    render(createElement(ModelsSection));
    return within(screen.getAllByRole('row')[1]).getAllByRole('cell')[2];
  }

  it('shows input, output and cache-hit prices with their labels and full precision', () => {
    const cell = renderPricing({ prompt: '0.15', completion: '0.60', input_cache_reads: '0.003' });
    expect(screen.getByRole('columnheader', { name: 'USD / 1M tokens' })).toBeInTheDocument();
    for (const text of ['Input', '$0.15', 'Output', '$0.60', 'Cache hit', '$0.003']) {
      expect(within(cell).getByText(text)).toBeInTheDocument();
    }
    expect(within(cell).queryByText('Cache write')).not.toBeInTheDocument();
  });

  it('does not turn an unknown price into a free rate', () => {
    const cell = renderPricing({ prompt: '0.15' });
    expect(within(cell).getByText('Output').parentElement).toHaveTextContent('Output—');
    expect(cell).not.toHaveTextContent('$0 /');
  });

  it('keeps explicitly configured zero rates and cache-write prices visible', () => {
    const cell = renderPricing({ prompt: '0', completion: '0', input_cache_writes: '0.25' });
    expect(within(cell).getAllByText('$0')).toHaveLength(2);
    expect(within(cell).getByText('Cache write').parentElement).toHaveTextContent('$0.25');
  });

  it.each(['0', '0.00'])('shows the input rate for cache rates configured as %s', (zero) => {
    const cell = renderPricing({
      prompt: '0.15',
      completion: '0.60',
      input_cache_reads: zero,
      input_cache_writes: zero,
    });
    expect(within(cell).getByText('Cache hit').parentElement).toHaveTextContent('Cache hit$0.15');
    expect(within(cell).getByText('Cache write').parentElement).toHaveTextContent(
      'Cache write$0.15',
    );
    expect(within(cell).queryByText(`$${zero}`)).not.toBeInTheDocument();
  });

  it('does not call zero cache rates free when the input rate is unknown', () => {
    const cell = renderPricing({
      completion: '0.60',
      input_cache_reads: '0',
      input_cache_writes: '0.00',
    });
    expect(within(cell).getByText('Cache hit').parentElement).toHaveTextContent('Cache hit—');
    expect(within(cell).getByText('Cache write').parentElement).toHaveTextContent('Cache write—');
    expect(within(cell).queryByText(/^\$0(?:\.00)?$/)).not.toBeInTheDocument();
  });

  it('shows a dash for an empty price table', () => {
    expect(renderPricing({})).toHaveTextContent(/^—$/);
  });
});
