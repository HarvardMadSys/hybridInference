// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ProviderRoutesTab } from './ProviderRoutesTab';

vi.mock('@/lib/api/admin', () => ({
  deleteProviderRoute: vi.fn(),
  listProviderKeys: vi.fn(),
  listProviderRoutes: vi.fn(),
  updateProviderRoute: vi.fn(),
  updateProviderRouteStrategy: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import {
  deleteProviderRoute,
  listProviderKeys,
  listProviderRoutes,
  updateProviderRoute,
  updateProviderRouteStrategy,
} from '@/lib/api/admin';

const providerOptions = [
  {
    provider: 'featherless',
    label: 'Featherless',
    kind: 'featherless',
    key_provider: 'featherless',
    default_base_url: 'https://api.featherless.ai/v1',
  },
  {
    provider: 'parasail',
    label: 'Parasail via OpenRouter',
    kind: 'openrouter[parasail]',
    key_provider: 'openrouter',
    default_base_url: 'https://openrouter.ai/api/v1',
  },
];

const route = {
  model_id: 'minimax-fast',
  strategy: 'routewise',
  route_id: 'minimax-fast:featherless-api',
  route_type: 'concurrency',
  provider: 'featherless',
  upstream_provider: 'featherless',
  key_provider: 'featherless',
  base_url: 'https://api.featherless.ai/v1',
  api_key_id: null,
  api_key: {
    id: null,
    provider: 'featherless',
    label: 'Provider default',
    key_prefix: null,
    source: 'default' as const,
  },
  provider_model_id: 'MiniMaxAI/MiniMax-M2.5',
  quota_limit: null,
  endpoint_id: 'minimax-fast:featherless-api',
  yaml_weight: 1,
  effective_weight: 1,
  source: 'yaml' as const,
  updated_at: null,
  updated_by: null,
};

describe('ProviderRoutesTab', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders routewise provider candidates without weight columns', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });

    render(<ProviderRoutesTab />);

    expect(await screen.findByText('minimax-fast')).toBeInTheDocument();
    expect(screen.getByText('featherless')).toBeInTheDocument();
    expect(screen.getByText('Default featherless pool')).toBeInTheDocument();
    expect(screen.queryByText('Effective')).not.toBeInTheDocument();
  });

  it('updates provider route target', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) => ({
      provider: provider ?? null,
      keys:
        provider === 'openrouter'
          ? [
            {
              id: 'key-1',
              provider: 'openrouter',
              key_prefix: 'sk-or...1234',
              label: 'staging',
              source: 'db',
              status: 'active',
              created_at: null,
            },
          ]
          : [],
    }));
    vi.mocked(updateProviderRoute).mockResolvedValue({
      ...route,
      provider: 'featherless',
      upstream_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db',
      },
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: null,
      endpoint_id: 'minimax-fast:openrouter[parasail]-api',
      source: 'override',
      updated_by: '127.0.0.1',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Override provider'), {
      target: { value: 'parasail' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://openrouter.ai/api/v1');
    fireEvent.click(screen.getByRole('button', { name: 'Verify & Apply' }));

    await waitFor(() => {
      expect(updateProviderRoute).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
        {
          upstream_provider: 'parasail',
          base_url: 'https://openrouter.ai/api/v1',
          api_key_id: 'key-1',
          provider_model_id: 'minimax/minimax-m2.5',
          quota_limit: null,
        },
      );
    });
    expect(await screen.findByText('parasail')).toBeInTheDocument();
  });

  it('submits local daily quota for quota provider overrides', async () => {
    const quotaRoute = {
      ...route,
      route_id: 'minimax-fast:chutes-api',
      route_type: 'quota',
      provider: 'chutes',
      upstream_provider: 'chutes',
      key_provider: 'chutes',
      base_url: 'https://llm.chutes.ai/v1',
      provider_model_id: 'MiniMaxAI/MiniMax-M2.5-TEE',
      quota_limit: 5000,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [
        {
          provider: 'chutes',
          label: 'Chutes',
          kind: 'chutes',
          key_provider: 'chutes',
          default_base_url: 'https://llm.chutes.ai/v1',
        },
        ...providerOptions,
      ],
      routes: [quotaRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });
    vi.mocked(updateProviderRoute).mockResolvedValue({
      ...quotaRoute,
      upstream_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: 8000,
      source: 'override',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Override provider'), {
      target: { value: 'parasail' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    fireEvent.change(screen.getByLabelText('Local daily quota'), {
      target: { value: '8000' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Verify & Apply' }));

    await waitFor(() => {
      expect(updateProviderRoute).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:chutes-api',
        {
          upstream_provider: 'parasail',
          base_url: 'https://openrouter.ai/api/v1',
          api_key_id: null,
          provider_model_id: 'minimax/minimax-m2.5',
          quota_limit: 8000,
        },
      );
    });
  });

  it('restores an override to the YAML route', async () => {
    const overrideRoute = {
      ...route,
      upstream_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db' as const,
      },
      provider_model_id: 'minimax/minimax-m2.5',
      source: 'override' as const,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      routes: [overrideRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });
    vi.mocked(deleteProviderRoute).mockResolvedValue(route);

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Restore YAML' }));

    await waitFor(() => {
      expect(deleteProviderRoute).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
      );
    });
    await waitFor(() => {
      expect(screen.getAllByText('Default featherless pool').length).toBeGreaterThan(0);
    });
    expect(screen.queryByText('Edit provider route')).not.toBeInTheDocument();
  });

  it('keeps the current override provider selectable when it is not a default option', async () => {
    const zaiRoute = {
      ...route,
      upstream_provider: 'zai',
      key_provider: 'zai',
      base_url: 'https://api.z.ai/api/paas/v4',
      provider_model_id: 'glm-4.5',
      source: 'override' as const,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      routes: [zaiRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'zai', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));

    expect(screen.getByLabelText('Override provider')).toHaveValue('zai');
    expect(screen.getByRole('option', { name: 'zai' })).toBeInTheDocument();
    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('zai');
    });
  });

  it('updates the selected model routing policy', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });
    vi.mocked(updateProviderRouteStrategy).mockResolvedValue({
      model_id: 'minimax-fast',
      strategy: 'fixed',
      provider_options: providerOptions,
      routes: [{ ...route, strategy: 'fixed' }],
    });

    render(<ProviderRoutesTab />);

    const select = await screen.findByLabelText('Routing policy');
    expect(select).toHaveValue('routewise');

    fireEvent.change(select, { target: { value: 'fixed' } });

    await waitFor(() => {
      expect(updateProviderRouteStrategy).toHaveBeenCalledWith('minimax-fast', 'fixed');
    });
    expect(screen.getByLabelText('Routing policy')).toHaveValue('fixed');
  });
});
