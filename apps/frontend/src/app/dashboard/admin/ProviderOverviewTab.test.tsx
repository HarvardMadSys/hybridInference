// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ProviderOverviewTab } from './ProviderOverviewTab';

vi.mock('@/lib/api/admin', () => ({
  createProviderDefinition: vi.fn(),
  deleteProviderDefinition: vi.fn(),
  listProviderDefinitions: vi.fn(),
  probeProviderDefinition: vi.fn(),
  updateProviderDefinition: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import { listProviderDefinitions } from '@/lib/api/admin';

describe('ProviderOverviewTab', () => {
  beforeEach(() => {
    vi.mocked(listProviderDefinitions).mockResolvedValue({
      adapter_kinds: ['openai_compat'],
      providers: [
        {
          provider: 'openrouter',
          display_name: 'OpenRouter',
          adapter_kind: 'openrouter',
          default_base_url: 'https://openrouter.ai/api/v1',
          source: 'built_in',
          status: 'active',
          keys_count: 1,
          models_count: 2,
          created_at: null,
          updated_at: null,
        },
        {
          provider: 'acme',
          display_name: 'Acme',
          adapter_kind: 'openai_compat',
          default_base_url: 'https://api.acme.test/v1',
          source: 'custom',
          status: 'active',
          keys_count: 2,
          models_count: 3,
          created_at: null,
          updated_at: null,
        },
      ],
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('renders the optimized registry table with summary and source badges', async () => {
    render(<ProviderOverviewTab />);

    expect(await screen.findByText('OpenRouter')).toBeInTheDocument();
    expect(screen.getByText('Acme')).toBeInTheDocument();
    expect(screen.getByText('2 providers')).toBeInTheDocument();
    expect(screen.getByText('3 keys')).toBeInTheDocument();
    expect(screen.getByText('5 models')).toBeInTheDocument();
    expect(screen.getByText('1 custom provider')).toBeInTheDocument();

    expect(screen.getByRole('columnheader', { name: 'Endpoint' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Usage' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Source' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Actions' })).toBeInTheDocument();
    expect(screen.getByText('Config')).toBeInTheDocument();
    expect(screen.getByText('Custom')).toBeInTheDocument();
    expect(screen.queryByText('Config managed')).not.toBeInTheDocument();
  });
});
