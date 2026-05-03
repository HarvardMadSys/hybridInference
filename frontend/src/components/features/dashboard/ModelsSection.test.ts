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
  it('hides codex_sub even when showInternalModels=true', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'codex_sub' }), true)).toBe(false);
  });

  it('hides claude_sub even when showInternalModels=true', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'claude_sub' }), true)).toBe(false);
  });

  it('with showInternalModels=false, hides Anthropic-owned models case-insensitively', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'Anthropic' }), false)).toBe(false);
  });

  it('with showInternalModels=false, hides OpenAI-owned models case-insensitively', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'OpenAI' }), false)).toBe(false);
  });

  it('with showInternalModels=true, shows an anthropic-owned model', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'anthropic' }), true)).toBe(true);
  });

  it('with showInternalModels=true, shows an openai-owned model', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'openai' }), true)).toBe(true);
  });

  it('with showInternalModels=false, shows an unrelated third-party model', () => {
    expect(isDashboardModelVisible(makeModel({ owned_by: 'mistral' }), false)).toBe(true);
  });
});
