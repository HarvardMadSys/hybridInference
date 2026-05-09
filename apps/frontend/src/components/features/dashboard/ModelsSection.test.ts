import { describe, expect, it } from 'vitest';

import type { ModelCatalogItem } from '@/lib/api/user';
import { isDashboardModelVisible } from './ModelsSection';

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
