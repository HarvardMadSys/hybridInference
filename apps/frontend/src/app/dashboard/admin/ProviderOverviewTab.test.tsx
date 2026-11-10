// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

import toast from 'react-hot-toast';
import {
  createProviderDefinition,
  deleteProviderDefinition,
  listProviderDefinitions,
  probeProviderDefinition,
  updateProviderDefinition,
} from '@/lib/api/admin';
import type { ProviderDefinitionItem } from '@/lib/api/admin';

const SECRET = 'typed-provider-key-value';

const ACME: ProviderDefinitionItem = {
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
};

const PROBE_OK = {
  ok: true,
  streaming: true,
  first_event_ttft_ms: 120,
  first_content_ttft_ms: 180,
  preview: 'hello',
};

function confirmDialog(): HTMLElement {
  return screen.getByRole('dialog');
}

function clickInDialog(name: string) {
  fireEvent.click(within(confirmDialog()).getByRole('button', { name }));
}

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
    expect(screen.getByRole('button', { name: 'Edit Acme' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete Acme' })).toBeDisabled();
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

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Acme' }));

    expect(screen.queryByLabelText('Type acme to confirm')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('opens deletion confirmation for an unused custom provider', async () => {
    const response = await vi.mocked(listProviderDefinitions)();
    vi.mocked(listProviderDefinitions).mockResolvedValue({
      ...response,
      providers: response.providers.map((provider) => ({ ...provider, models_count: 0 })),
    });
    render(<ProviderOverviewTab onManageKeys={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Acme' }));

    expect(screen.getByRole('dialog', { name: 'Delete provider “Acme”?' })).toHaveTextContent(
      'This cannot be undone.',
    );
    expect(screen.getByLabelText('Type acme to confirm')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete provider' })).toBeDisabled();
  });

  describe('deleting', () => {
    beforeEach(() => {
      vi.mocked(listProviderDefinitions).mockResolvedValue({
        adapter_kinds: ['openai_compat'],
        providers: [{ ...ACME, models_count: 0 }],
      });
    });

    it('writes nothing on Cancel and deletes once after the slug is typed', async () => {
      vi.mocked(deleteProviderDefinition).mockResolvedValue({ provider: 'acme', deleted_keys: 2 });
      render(<ProviderOverviewTab onManageKeys={vi.fn()} />);

      fireEvent.click(await screen.findByRole('button', { name: 'Delete Acme' }));
      expect(confirmDialog()).toHaveTextContent('2 keys');
      clickInDialog('Cancel');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(deleteProviderDefinition).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Delete Acme' }));
      fireEvent.change(screen.getByLabelText('Type acme to confirm'), {
        target: { value: 'acm' },
      });
      expect(screen.getByRole('button', { name: 'Delete provider' })).toBeDisabled();
      clickInDialog('Delete provider');
      expect(deleteProviderDefinition).not.toHaveBeenCalled();

      fireEvent.change(screen.getByLabelText('Type acme to confirm'), {
        target: { value: 'acme' },
      });
      clickInDialog('Delete provider');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(deleteProviderDefinition).mock.calls).toEqual([['acme']]);
      expect(toast.success).toHaveBeenCalledWith('Provider deleted');
      expect(listProviderDefinitions).toHaveBeenCalledTimes(2);
    });

    it('keeps the delete confirmation open with the error when deleting fails', async () => {
      vi.mocked(deleteProviderDefinition).mockRejectedValueOnce(new Error('provider in use'));
      render(<ProviderOverviewTab onManageKeys={vi.fn()} />);

      fireEvent.click(await screen.findByRole('button', { name: 'Delete Acme' }));
      fireEvent.change(screen.getByLabelText('Type acme to confirm'), {
        target: { value: 'acme' },
      });
      clickInDialog('Delete provider');

      expect(await within(confirmDialog()).findByRole('alert')).toHaveTextContent(
        'provider in use',
      );
      expect(deleteProviderDefinition).toHaveBeenCalledTimes(1);
    });
  });

  describe('adding', () => {
    async function fillAddForm() {
      render(<ProviderOverviewTab onManageKeys={vi.fn()} />);
      await screen.findByText('Acme');
      fireEvent.click(screen.getByRole('button', { name: 'Add Provider' }));
      fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'New Co' } });
      fireEvent.change(screen.getByLabelText('Probe model ID'), { target: { value: 'm-1' } });
      fireEvent.change(screen.getByLabelText('Base URL'), {
        target: { value: 'https://api.new.test/v1' },
      });
      fireEvent.change(screen.getByLabelText('API key'), { target: { value: SECRET } });
    }

    it('probes directly, then adds the provider on Add provider, with no confirmation', async () => {
      vi.mocked(probeProviderDefinition).mockResolvedValue(PROBE_OK);
      vi.mocked(createProviderDefinition).mockResolvedValue({ ...ACME, provider: 'new_co' });
      await fillAddForm();
      // Nothing is added before the probe has checked the form.
      expect(screen.getByRole('button', { name: 'Add provider' })).toBeDisabled();
      // Every added provider is OpenAI-compatible: said once, not offered as a choice.
      expect(screen.queryByLabelText('Adapter')).toBeNull();
      expect(screen.getByLabelText('Base URL')).toHaveAccessibleDescription(
        "The provider's OpenAI-compatible API.",
      );

      fireEvent.click(screen.getByRole('button', { name: 'Probe' }));
      expect(await screen.findByText('Probe successful')).toBeInTheDocument();
      // The probe stores nothing, so it runs without a confirmation.
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(probeProviderDefinition).toHaveBeenCalledWith({
        default_base_url: 'https://api.new.test/v1',
        api_key: SECRET,
        probe_model_id: 'm-1',
      });

      expect(createProviderDefinition).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Add provider' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      await waitFor(() => expect(screen.queryByLabelText('Display name')).toBeNull());
      expect(vi.mocked(createProviderDefinition).mock.calls).toEqual([
        [
          {
            provider: 'new_co',
            display_name: 'New Co',
            adapter_kind: 'openai_compat',
            default_base_url: 'https://api.new.test/v1',
            api_key: SECRET,
            api_key_label: null,
            probe_model_id: 'm-1',
          },
        ],
      ]);
      expect(toast.success).toHaveBeenCalledWith('Provider added');
    });

    it('holds the form while the provider is being added, and adds it once', async () => {
      let finish!: (value: ProviderDefinitionItem) => void;
      vi.mocked(probeProviderDefinition).mockResolvedValue(PROBE_OK);
      vi.mocked(createProviderDefinition).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      await fillAddForm();
      fireEvent.click(screen.getByRole('button', { name: 'Probe' }));
      await screen.findByText('Probe successful');
      fireEvent.click(screen.getByRole('button', { name: 'Add provider' }));

      const adding = screen.getByRole('button', { name: 'Adding…' });
      expect(adding).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
      expect(screen.getByLabelText('Display name')).toBeDisabled();
      expect(screen.getByLabelText('API key')).toBeDisabled();
      fireEvent.click(adding);
      expect(createProviderDefinition).toHaveBeenCalledTimes(1);

      finish({ ...ACME, provider: 'new_co' });
      await waitFor(() => expect(screen.queryByLabelText('Display name')).toBeNull());
      expect(createProviderDefinition).toHaveBeenCalledTimes(1);
    });

    it('keeps the form with a masked error when creating fails, and adds it on a retry', async () => {
      vi.mocked(probeProviderDefinition).mockResolvedValue(PROBE_OK);
      vi.mocked(createProviderDefinition)
        .mockRejectedValueOnce(new Error(`upstream refused ${SECRET}`))
        .mockResolvedValueOnce({ ...ACME, provider: 'new_co' });
      await fillAddForm();
      fireEvent.click(screen.getByRole('button', { name: 'Probe' }));
      await screen.findByText('Probe successful');
      fireEvent.click(screen.getByRole('button', { name: 'Add provider' }));

      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent('upstream refused ••••');
      expect(alert).not.toHaveTextContent(SECRET);
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByLabelText('Display name')).toHaveValue('New Co');
      expect(createProviderDefinition).toHaveBeenCalledTimes(1);

      fireEvent.click(screen.getByRole('button', { name: 'Add provider' }));
      await waitFor(() => expect(screen.queryByLabelText('Display name')).toBeNull());
      expect(createProviderDefinition).toHaveBeenCalledTimes(2);
      expect(toast.success).toHaveBeenCalledWith('Provider added');
    });
  });

  describe('editing', () => {
    beforeEach(() => {
      vi.mocked(listProviderDefinitions).mockResolvedValue({
        adapter_kinds: ['openai_compat'],
        providers: [{ ...ACME, models_count: 0 }],
      });
    });

    async function openEditor() {
      render(<ProviderOverviewTab onManageKeys={vi.fn()} />);
      fireEvent.click(await screen.findByRole('button', { name: 'Edit Acme' }));
    }

    function save() {
      fireEvent.click(screen.getByRole('button', { name: 'Save changes to Acme' }));
    }

    it('shows the saved values read-only until Edit opens the editor', async () => {
      render(<ProviderOverviewTab onManageKeys={vi.fn()} />);
      await screen.findByText('Acme');
      expect(screen.queryByRole('textbox')).toBeNull();

      fireEvent.click(screen.getByRole('button', { name: 'Edit Acme' }));
      expect(screen.getByLabelText('Display name')).toHaveValue('Acme');
      expect(screen.getByLabelText('Base URL')).toHaveValue('https://api.acme.test/v1');
      expect(screen.getByText('No changes yet')).toBeVisible();
      expect(screen.getByRole('button', { name: 'Save changes to Acme' })).toBeDisabled();
    });

    it('keeps edits as drafts: Cancel writes nothing, and a value typed back is no change', async () => {
      await openEditor();
      const name = screen.getByLabelText('Display name');
      fireEvent.change(name, { target: { value: 'Acme Corp' } });
      expect(screen.getByText('1 unsaved change')).toBeVisible();
      fireEvent.change(name, { target: { value: 'Acme' } });
      expect(screen.getByRole('button', { name: 'Save changes to Acme' })).toBeDisabled();

      fireEvent.change(name, { target: { value: 'Acme Corp' } });
      fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Acme' }));
      expect(screen.queryByLabelText('Display name')).toBeNull();
      expect(updateProviderDefinition).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Edit Acme' }));
      expect(screen.getByLabelText('Display name')).toHaveValue('Acme');
    });

    it('saves the changed fields once on Save, with no confirmation', async () => {
      vi.mocked(updateProviderDefinition).mockResolvedValue({ ...ACME, display_name: 'Acme Corp' });
      await openEditor();
      fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Acme Corp' } });
      expect(updateProviderDefinition).not.toHaveBeenCalled();

      save();
      expect(screen.queryByRole('dialog')).toBeNull();
      await waitFor(() => expect(screen.queryByLabelText('Display name')).toBeNull());
      expect(vi.mocked(updateProviderDefinition).mock.calls).toEqual([
        ['acme', { display_name: 'Acme Corp' }],
      ]);
      expect(toast.success).toHaveBeenCalledWith('Provider updated');
    });

    it('holds a second Save, Cancel and the fields while the save is in flight', async () => {
      let finish!: (value: ProviderDefinitionItem) => void;
      vi.mocked(updateProviderDefinition).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      await openEditor();
      fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Acme Corp' } });
      save();

      const saving = screen.getByRole('button', { name: 'Save changes to Acme' });
      expect(saving).toHaveTextContent('Saving…');
      expect(saving).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Cancel editing Acme' })).toBeDisabled();
      expect(screen.getByLabelText('Display name')).toBeDisabled();
      fireEvent.submit(screen.getByRole('form', { name: 'Edit provider Acme' }));
      expect(updateProviderDefinition).toHaveBeenCalledTimes(1);

      finish({ ...ACME, display_name: 'Acme Corp' });
      await waitFor(() => expect(screen.queryByLabelText('Display name')).toBeNull());
      expect(updateProviderDefinition).toHaveBeenCalledTimes(1);
    });

    it('keeps the editor and drafts after a failure, says why, and saves on a retry', async () => {
      vi.mocked(updateProviderDefinition)
        .mockRejectedValueOnce(new Error('write failed'))
        .mockResolvedValueOnce({ ...ACME, display_name: 'Acme Corp' });
      await openEditor();
      fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Acme Corp' } });
      save();

      expect(await screen.findByRole('alert')).toHaveTextContent('write failed');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByLabelText('Display name')).toHaveValue('Acme Corp');

      save();
      await waitFor(() => expect(screen.queryByLabelText('Display name')).toBeNull());
      expect(updateProviderDefinition).toHaveBeenCalledTimes(2);
    });

    it('masks the probe key in a failed save of a new Base URL', async () => {
      vi.mocked(probeProviderDefinition).mockResolvedValue(PROBE_OK);
      vi.mocked(updateProviderDefinition).mockRejectedValueOnce(
        new Error(`upstream refused ${SECRET}`),
      );
      await openEditor();
      fireEvent.change(screen.getByLabelText('Base URL'), {
        target: { value: 'https://api2.acme.test/v1' },
      });
      fireEvent.change(screen.getByLabelText('API key'), { target: { value: SECRET } });
      fireEvent.change(screen.getByLabelText('Probe model ID'), { target: { value: 'm-1' } });
      fireEvent.click(screen.getByRole('button', { name: 'Probe' }));
      await screen.findByText('Probe successful');
      save();

      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent('upstream refused ••••');
      expect(alert).not.toHaveTextContent(SECRET);
      expect(screen.getByLabelText('API key')).toHaveValue(SECRET);
    });

    it('requires a probe for a new Base URL, then saves it with the probe key and model', async () => {
      vi.mocked(probeProviderDefinition).mockResolvedValue(PROBE_OK);
      vi.mocked(updateProviderDefinition).mockResolvedValue({
        ...ACME,
        default_base_url: 'https://api2.acme.test/v1',
      });
      await openEditor();
      fireEvent.change(screen.getByLabelText('Base URL'), {
        target: { value: 'https://api2.acme.test/v1' },
      });
      fireEvent.change(screen.getByLabelText('API key'), { target: { value: SECRET } });
      fireEvent.change(screen.getByLabelText('Probe model ID'), { target: { value: 'm-1' } });

      // The key and model only check the new Base URL, and the editor says so.
      expect(
        screen.getByText('The new Base URL is probed with this key and model; neither is stored.'),
      ).toBeInTheDocument();

      save();
      expect(screen.getByRole('alert')).toHaveTextContent('Probe the new Base URL');
      expect(updateProviderDefinition).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Probe' }));
      expect(await screen.findByText('Probe successful')).toBeInTheDocument();
      expect(screen.queryByRole('dialog')).toBeNull();

      save();
      await waitFor(() => expect(screen.queryByLabelText('Base URL')).toBeNull());
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(vi.mocked(updateProviderDefinition).mock.calls).toEqual([
        [
          'acme',
          {
            display_name: 'Acme',
            default_base_url: 'https://api2.acme.test/v1',
            api_key: SECRET,
            probe_model_id: 'm-1',
          },
        ],
      ]);
    });
  });
});
