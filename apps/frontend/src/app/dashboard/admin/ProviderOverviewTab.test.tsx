// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
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

  it('renders the registry table with usage summary and no source labels', async () => {
    render(<ProviderOverviewTab onManageKeys={vi.fn()} />);

    expect(await screen.findByText('OpenRouter')).toBeInTheDocument();
    expect(screen.getByText('Acme')).toBeInTheDocument();
    expect(screen.getByText('2 providers')).toBeInTheDocument();
    expect(screen.getByText('3 keys')).toBeInTheDocument();
    expect(screen.getByText('5 models')).toBeInTheDocument();
    expect(screen.queryByText('1 custom provider')).not.toBeInTheDocument();

    expect(screen.getByRole('columnheader', { name: 'Endpoint' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Resources' })).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Source' })).not.toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Actions' })).toBeInTheDocument();
    expect(screen.queryByText('Config')).not.toBeInTheDocument();
    expect(screen.queryByText('Custom')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled();
    expect(screen.getByText('Remove linked model routes before deleting.')).toBeInTheDocument();
  });

  it('opens key management for the selected provider regardless of source', async () => {
    const onManageKeys = vi.fn();
    render(<ProviderOverviewTab onManageKeys={onManageKeys} />);

    for (const [name, provider] of [
      ['OpenRouter', 'openrouter'],
      ['Acme', 'acme'],
    ]) {
      const row = (await screen.findByText(name)).closest('tr')!;
      fireEvent.click(within(row).getByRole('button', { name: 'Manage keys' }));
      expect(onManageKeys).toHaveBeenLastCalledWith(provider);
    }
  });

  it('does not open deletion confirmation for a provider with linked models', async () => {
    render(<ProviderOverviewTab onManageKeys={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));

    expect(screen.queryByLabelText('Type acme to confirm')).not.toBeInTheDocument();
  });

  it('opens deletion confirmation for an unused custom provider', async () => {
    const response = await vi.mocked(listProviderDefinitions)();
    vi.mocked(listProviderDefinitions).mockResolvedValue({
      ...response,
      providers: response.providers.map((provider) => ({ ...provider, models_count: 0 })),
    });
    render(<ProviderOverviewTab onManageKeys={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));

    expect(screen.getByLabelText('Type acme to confirm')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete provider' })).toBeDisabled();
  });
});
