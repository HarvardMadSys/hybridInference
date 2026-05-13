// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

const replaceMock = vi.fn();

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: replaceMock }),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    listRuntimeSettings: vi.fn(async () => ({ settings: [] })),
    listSignupAllowedDomains: vi.fn(async () => ({ domains: [] })),
    listModelVisibility: vi.fn(async () => ({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    })),
    updateModelVisibility: vi.fn(async () => ({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: 'internal',
      effective_required_role: 'internal',
    })),
    listRoutewiseSettings: vi.fn(async () => ({
      settings: [
        {
          key: 'decision_rule',
          value: 'pd',
          value_type: 'str',
          default_value: 'pd',
          description: 'Decision rule used by Routewise.',
        },
      ],
    })),
    updateRoutewiseSetting: vi.fn(),
    listRouteWeights: vi.fn(async () => [
      {
        model_id: 'gpt-4o-mini',
        strategy: 'fixed',
        endpoint_id: 'gpt-4o-mini:remote',
        provider: 'remote',
        base_url: 'https://api.example.test',
        yaml_weight: 2,
        override_weight: null,
        effective_weight: 2,
      },
    ]),
    setRouteWeight: vi.fn(),
    clearRouteWeight: vi.fn(),
  };
});

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

describe('SettingsTab model visibility', () => {
  afterEach(() => {
    cleanup();
    replaceMock.mockClear();
  });

  it('renders the section inside Admin Settings and shows update toasts', async () => {
    const api = await import('@/lib/api/admin');
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SettingsTab />
      </QueryClientProvider>,
    );

    expect(await screen.findByText('Model Visibility')).toBeInTheDocument();
    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(api.updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
    });
    expect(await screen.findByText('Updated visibility for gpt-4o-mini.')).toBeInTheDocument();
  });

  it('renders routing weights behind a Settings subtab', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SettingsTab />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('tab', { name: 'General' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    const routingTab = screen.getByRole('tab', { name: 'Routing' });
    expect(routingTab).toHaveAttribute('aria-selected', 'false');

    fireEvent.click(routingTab);

    expect(routingTab).toHaveAttribute('aria-selected', 'true');
    expect(replaceMock).toHaveBeenCalledWith('/dashboard/admin/settings?tab=routing', {
      scroll: false,
    });
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Routing Weights' }),
    ).toBeInTheDocument();
    expect(await screen.findByText('gpt-4o-mini:remote')).toBeInTheDocument();
  });

  it('renders a server-provided routing subtab without reading window location', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SettingsTab initialSubtab="routing" />
      </QueryClientProvider>,
    );

    expect(screen.getByRole('tab', { name: 'Routing' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByText('Routing Weights')).toBeInTheDocument();
  });

  it('renders and selects the routewise subtab', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SettingsTab />
      </QueryClientProvider>,
    );

    const routewiseTab = await screen.findByRole('tab', { name: 'Routewise' });
    expect(routewiseTab).toHaveAttribute('aria-selected', 'false');

    fireEvent.click(routewiseTab);

    expect(routewiseTab).toHaveAttribute('aria-selected', 'true');
    expect(replaceMock).toHaveBeenCalledWith('/dashboard/admin/settings?tab=routewise', {
      scroll: false,
    });
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Routewise Settings' }),
    ).toBeInTheDocument();
  });

  it('renders a server-provided routewise subtab without reading window location', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SettingsTab initialSubtab="routewise" />
      </QueryClientProvider>,
    );

    expect(screen.getByRole('tab', { name: 'Routewise' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByText('Routewise Settings')).toBeInTheDocument();
  });

  it('resyncs the selected subtab when initialSubtab changes after mount', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <SettingsTab initialSubtab="general" />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('tab', { name: 'General' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByRole('tabpanel', { name: 'General' })).toBeInTheDocument();

    rerender(
      <QueryClientProvider client={queryClient}>
        <SettingsTab initialSubtab="routewise" />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'Routewise' })).toHaveAttribute('aria-selected', 'true');
    });

    const routewisePanel = screen.getByRole('tabpanel', { name: 'Routewise' });
    expect(routewisePanel).toHaveAttribute('id', 'admin-settings-routewise-panel');
    expect(within(routewisePanel).getByRole('heading', { level: 2, name: 'Routewise Settings' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'General' })).toHaveAttribute('aria-selected', 'false');
  });

  it('links tabs to accessible panels', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SettingsTab />
      </QueryClientProvider>,
    );

    const generalTab = await screen.findByRole('tab', { name: 'General' });
    expect(generalTab).toHaveAttribute('aria-controls', 'admin-settings-general-panel');
    expect(screen.getByRole('tabpanel', { name: 'General' })).toHaveAttribute(
      'id',
      'admin-settings-general-panel',
    );

    const routingTab = screen.getByRole('tab', { name: 'Routing' });
    expect(routingTab).toHaveAttribute('aria-controls', 'admin-settings-routing-panel');
    fireEvent.click(routingTab);

    expect(screen.getByRole('tabpanel', { name: 'Routing' })).toHaveAttribute(
      'id',
      'admin-settings-routing-panel',
    );

    const routewiseTab = screen.getByRole('tab', { name: 'Routewise' });
    expect(routewiseTab).toHaveAttribute('aria-controls', 'admin-settings-routewise-panel');
    fireEvent.click(routewiseTab);

    expect(screen.getByRole('tabpanel', { name: 'Routewise' })).toHaveAttribute(
      'id',
      'admin-settings-routewise-panel',
    );
  });
});
