// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConfigEntry, ConfigResponse, LiveConfigStatus } from '@/lib/api/config';
import { APIError } from '@/lib/utils/errors';
import { chooseOption } from '@/test-utils/select';

const navigateToMock = vi.fn();

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/providers/models',
  useRouter: () => ({ replace: vi.fn(), refresh: vi.fn() }),
}));

vi.mock('@/lib/utils/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/navigation')>()),
  navigateTo: (url: string) => navigateToMock(url),
}));

vi.mock('@/lib/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/config')>()),
  getConfig: vi.fn(),
  patchConfig: vi.fn(),
  getLiveConfigStatus: vi.fn(),
  restartBackend: vi.fn(),
  waitForBackendRestart: vi.fn(),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
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
    listModelConcurrency: vi.fn(async () => ({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    })),
    updateModelConcurrency: vi.fn(async () => ({ model_id: 'gpt-4o-mini', exempt: true })),
  };
});

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import toast from 'react-hot-toast';
import {
  getConfig,
  getLiveConfigStatus,
  patchConfig,
  restartBackend,
  waitForBackendRestart,
} from '@/lib/api/config';
import { updateModelConcurrency, updateModelVisibility } from '@/lib/api/admin';

import { ModelsTab } from '../ModelsTab';
import { LEAVE_MESSAGE } from '../useLeaveGuard';

function entry(overrides: Partial<ConfigEntry>): ConfigEntry {
  return {
    key: 'SETTING',
    category: 'general',
    description: '',
    type: 'str',
    secret: false,
    required: false,
    missing: false,
    is_set: true,
    value: '',
    default: null,
    source: 'database',
    restart_required: false,
    pending_restart: false,
    environment_ignored: false,
    immutable: false,
    setup: false,
    one_time: false,
    custom: false,
    invalid: null,
    used_by: [],
    updated_at: null,
    updated_by: null,
    ...overrides,
  };
}

const CATALOG =
  '{"models": [{"id": "gpt-4o-mini", "name": "GPT-4o mini", "context_length": 128000}]}';
const v = (revision: number) => ({ epoch: 'e1', revision });

/** The catalog in its category, beside categories Settings shows, one of them missing a value. */
function config(overrides: Partial<ConfigResponse> = {}): ConfigResponse {
  return {
    categories: [
      { id: 'models', label: 'Model catalog', description: 'Model metadata and routes.' },
      { id: 'application', label: 'Application', description: 'Site documents.' },
      { id: 'email', label: 'Email', description: 'Outgoing mail.' },
    ],
    entries: [
      entry({
        key: 'MODEL_CATALOG',
        category: 'models',
        type: 'text',
        value: CATALOG,
        description: 'Model catalog as JSON.',
        one_time: true,
        restart_required: true,
        security_relevant: true,
        document: true,
      }),
      entry({
        key: 'SITE_BRANDING',
        category: 'application',
        type: 'text',
        value: '{}',
        security_relevant: true,
      }),
      entry({
        key: 'SMTP_HOST',
        category: 'email',
        required: true,
        missing: true,
        is_set: false,
        value: null,
        source: 'default',
      }),
    ],
    missing: ['SMTP_HOST'],
    pending_restart: [],
    restart_supported: true,
    source_version: v(1),
    ...overrides,
  };
}

const LIVE_STATUS: LiveConfigStatus = {
  database: true,
  source: 'app_config',
  version: v(1),
  this_instance: 'p1',
  stale_after_seconds: 60,
  read_ok: true,
  unknown_groups: [],
  groups: [],
};

async function renderModels() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ModelsTab />
    </QueryClientProvider>,
  );
  // The per-model overrides arrive on their own.
  await screen.findByRole('combobox', { name: 'Runtime override for gpt-4o-mini' });
  await screen.findByLabelText('Concurrency exemption for gpt-4o-mini');
}

/** The page's sections, top to bottom, by their headings. */
function sectionTitles() {
  return screen
    .getAllByRole('region')
    .filter((region) => region.hasAttribute('aria-labelledby'))
    .map((region) => document.getElementById(region.getAttribute('aria-labelledby')!)?.textContent);
}

beforeEach(() => {
  vi.mocked(getConfig).mockResolvedValue(config());
  vi.mocked(getLiveConfigStatus).mockResolvedValue(LIVE_STATUS);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Admin → Providers → Models', () => {
  it('holds the model catalog and the per-model overrides, and nothing else of the configuration', async () => {
    await renderModels();

    expect(await screen.findByRole('region', { name: 'Model catalog' })).toBeInTheDocument();
    expect(sectionTitles()).toEqual([
      'Model catalog',
      'Model Visibility',
      'Model Concurrency Limit',
    ]);
    // The catalog, one field per value.
    const catalog = within(screen.getByRole('group', { name: 'MODEL_CATALOG' }));
    expect(
      catalog.getAllByRole('textbox').map((field) => (field as HTMLInputElement).value),
    ).toEqual(['gpt-4o-mini', 'GPT-4o mini', '128000']);
    expect(catalog.getByRole('textbox', { name: 'models[0].context_length' })).toHaveValue(
      '128000',
    );
    // Settings' categories, and its list of missing settings, stay on Settings.
    expect(screen.queryByLabelText('SITE_BRANDING')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('SMTP_HOST')).not.toBeInTheDocument();
    expect(screen.queryByText(/required setting is missing/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Search settings')).not.toBeInTheDocument();
    // Models are created, and their routes changed, on Routing.
    expect(screen.getByRole('link', { name: 'Routing' })).toHaveAttribute(
      'href',
      '/dashboard/admin/routing',
    );
  });

  it('says beside the catalog how live route edits stand', async () => {
    await renderModels();

    const catalog = await screen.findByRole('region', { name: 'Model catalog' });
    expect(
      await within(catalog).findByText(/^Route edits apply across registered backend processes\./),
    ).toBeInTheDocument();
    expect(getLiveConfigStatus).toHaveBeenCalled();
  });

  it('saves the catalog only after the administrator confirms with a password', async () => {
    vi.mocked(patchConfig).mockResolvedValueOnce(
      config({ source_version: v(2), saved_version: v(2), pending_restart: ['MODEL_CATALOG'] }),
    );
    await renderModels();
    const catalog = await screen.findByRole('region', { name: 'Model catalog' });
    // The document saved whole, indented, with the field's new value as its type.
    const edited = JSON.stringify(
      { models: [{ id: 'gpt-4o-mini', name: 'GPT-4o mini', context_length: 64000 }] },
      null,
      2,
    );

    fireEvent.change(within(catalog).getByRole('textbox', { name: 'models[0].context_length' }), {
      target: { value: '64000' },
    });
    fireEvent.click(within(catalog).getByRole('button', { name: 'Save changes to Model catalog' }));
    const dialog = await screen.findByRole('dialog', {
      name: 'Save security settings in Model catalog?',
    });
    expect(patchConfig).not.toHaveBeenCalled();
    fireEvent.change(within(dialog).getByLabelText('Your password'), {
      target: { value: 'admin-password' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(1));
    expect(vi.mocked(patchConfig).mock.calls[0]).toEqual([
      { values: { MODEL_CATALOG: edited }, password: 'admin-password' },
      { expectedVersion: v(1) },
    ]);
    expect(toast.success).toHaveBeenCalledWith('Saved Model catalog.');
    // The catalog applies after a restart, which this page offers.
    expect(await screen.findByRole('region', { name: 'Restart pending' })).toHaveTextContent(
      'MODEL_CATALOG',
    );
  });

  it('restarts the backend for a saved catalog, then loads this tab afresh', async () => {
    vi.mocked(getConfig).mockResolvedValue(config({ pending_restart: ['MODEL_CATALOG'] }));
    vi.mocked(restartBackend).mockResolvedValue({ restarting: true, instance: 'old-process' });
    vi.mocked(waitForBackendRestart).mockResolvedValue('restarted');
    await renderModels();

    const notice = await screen.findByRole('region', { name: 'Restart pending' });
    fireEvent.click(within(notice).getByRole('button', { name: 'Restart backend' }));
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Restart the backend?' })).getByRole('button', {
        name: 'Restart backend',
      }),
    );

    await waitFor(() =>
      expect(navigateToMock).toHaveBeenCalledWith('/dashboard/admin/providers/models'),
    );
    expect(restartBackend).toHaveBeenCalledTimes(1);
  });

  it("saves a model's visibility override and says so", async () => {
    await renderModels();

    chooseOption('Runtime override for gpt-4o-mini', 'internal');
    expect(updateModelVisibility).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Model Visibility' }));

    await waitFor(() =>
      expect(updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal'),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Updated visibility for gpt-4o-mini.'),
    );
  });

  it("saves a model's concurrency exemption and says so", async () => {
    await renderModels();

    fireEvent.click(screen.getByLabelText('Concurrency exemption for gpt-4o-mini'));
    expect(updateModelConcurrency).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole('button', { name: 'Save changes to Model Concurrency Limit' }),
    );

    await waitFor(() => expect(updateModelConcurrency).toHaveBeenCalledWith('gpt-4o-mini', true));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Updated concurrency exemption for gpt-4o-mini.'),
    );
  });

  it('keeps the overrides when the backend has no configuration API', async () => {
    vi.mocked(getConfig).mockRejectedValue(new APIError('HTTP_404', 'Not Found', 404));
    await renderModels();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'does not support database-backed configuration',
    );
    expect(sectionTitles()).toEqual(['Model Visibility', 'Model Concurrency Limit']);
  });

  it('asks before leaving with an unsaved change, in the catalog or an override', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    window.history.replaceState(null, '', '/dashboard/admin/providers/models');
    await renderModels();
    const routing = screen.getByRole('link', { name: 'Routing' });

    // Nothing unsaved: no question.
    fireEvent.click(routing);
    expect(confirm).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole('textbox', { name: 'models[0].context_length' }), {
      target: { value: '64000' },
    });
    expect(fireEvent.click(routing)).toBe(false);
    expect(confirm).toHaveBeenCalledWith(LEAVE_MESSAGE);
    // Another Providers section is a page of its own as well.
    const keys = document.body.appendChild(document.createElement('a'));
    keys.href = '/dashboard/admin/providers/keys';
    expect(fireEvent.click(keys)).toBe(false);
    keys.remove();

    // Back to the saved value, then an override left unsaved.
    fireEvent.change(screen.getByRole('textbox', { name: 'models[0].context_length' }), {
      target: { value: '128000' },
    });
    confirm.mockClear();
    fireEvent.click(routing);
    expect(confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText('Concurrency exemption for gpt-4o-mini'));
    expect(fireEvent.click(routing)).toBe(false);
    expect(confirm).toHaveBeenCalledTimes(1);
    confirm.mockRestore();
  });

  it('points to Settings when the backend keeps the catalog with the other documents', async () => {
    const old = config();
    vi.mocked(getConfig).mockResolvedValue({
      ...old,
      categories: old.categories.filter((category) => category.id !== 'models'),
      entries: old.entries.map((item) =>
        item.key === 'MODEL_CATALOG' ? { ...item, category: 'application' } : item,
      ),
    });
    await renderModels();

    expect(await screen.findByText(/This backend lists the model catalog/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Settings → Site & Setup' })).toHaveAttribute(
      'href',
      '/dashboard/admin/settings?tab=setup',
    );
    expect(screen.queryByRole('region', { name: 'Model catalog' })).not.toBeInTheDocument();
  });
});
