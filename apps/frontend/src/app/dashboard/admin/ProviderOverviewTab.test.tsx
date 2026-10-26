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

    it('probes directly, then reviews the new provider without its key before creating it', async () => {
      vi.mocked(probeProviderDefinition).mockResolvedValue(PROBE_OK);
      vi.mocked(createProviderDefinition).mockResolvedValue({ ...ACME, provider: 'new_co' });
      await fillAddForm();
      expect(screen.getByRole('button', { name: 'Review provider' })).toBeDisabled();
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

      fireEvent.click(screen.getByRole('button', { name: 'Review provider' }));
      const review = screen.getByRole('dialog', { name: 'Add provider “New Co”?' });
      expect(review).toHaveTextContent('Slugnew_co');
      expect(review).not.toHaveTextContent('Adapter');
      expect(review).toHaveTextContent('Base URLhttps://api.new.test/v1');
      expect(review).toHaveTextContent('Probe model IDm-1');
      expect(review).toHaveTextContent('API keySet (hidden)');
      expect(review).toHaveTextContent('Key label(none)');
      expect(review).not.toHaveTextContent(SECRET);
      expect(createProviderDefinition).not.toHaveBeenCalled();

      clickInDialog('Back to editing');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByLabelText('Display name')).toHaveValue('New Co');
      expect(createProviderDefinition).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Review provider' }));
      clickInDialog('Add provider');
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

    it('keeps the review open with a masked error when creating fails', async () => {
      vi.mocked(probeProviderDefinition).mockResolvedValue(PROBE_OK);
      vi.mocked(createProviderDefinition).mockRejectedValueOnce(
        new Error(`upstream refused ${SECRET}`),
      );
      await fillAddForm();
      fireEvent.click(screen.getByRole('button', { name: 'Probe' }));
      await screen.findByText('Probe successful');
      fireEvent.click(screen.getByRole('button', { name: 'Review provider' }));
      clickInDialog('Add provider');

      expect(await within(confirmDialog()).findByRole('alert')).toHaveTextContent(
        'upstream refused ••••',
      );
      expect(confirmDialog()).not.toHaveTextContent(SECRET);
      expect(screen.getByLabelText('Display name')).toHaveValue('New Co');
      expect(createProviderDefinition).toHaveBeenCalledTimes(1);
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

    it('lists old -> new, keeps drafts on Back, and saves once on Confirm', async () => {
      vi.mocked(updateProviderDefinition).mockResolvedValue({ ...ACME, display_name: 'Acme Corp' });
      await openEditor();
      fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Acme Corp' } });
      save();

      const review = screen.getByRole('dialog', { name: 'Save 1 change to Acme?' });
      expect(review).toHaveTextContent('Display nameAcme → Acme Corp');
      expect(updateProviderDefinition).not.toHaveBeenCalled();

      clickInDialog('Back to editing');
      expect(screen.getByLabelText('Display name')).toHaveValue('Acme Corp');
      expect(updateProviderDefinition).not.toHaveBeenCalled();

      save();
      clickInDialog('Confirm save');
      await waitFor(() => expect(screen.queryByLabelText('Display name')).toBeNull());
      expect(vi.mocked(updateProviderDefinition).mock.calls).toEqual([
        ['acme', { display_name: 'Acme Corp' }],
      ]);
      expect(toast.success).toHaveBeenCalledWith('Provider updated');
    });

    it('blocks a second Confirm and closing while the save is in flight', async () => {
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
      clickInDialog('Confirm save');

      expect(within(confirmDialog()).getByRole('button', { name: 'Saving…' })).toBeDisabled();
      expect(
        within(confirmDialog()).getByRole('button', { name: 'Back to editing' }),
      ).toBeDisabled();
      fireEvent.click(within(confirmDialog()).getByRole('button', { name: 'Saving…' }));
      fireEvent.keyDown(window, { key: 'Escape' });
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Cancel editing Acme' })).toBeDisabled();
      expect(updateProviderDefinition).toHaveBeenCalledTimes(1);

      finish({ ...ACME, display_name: 'Acme Corp' });
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(updateProviderDefinition).toHaveBeenCalledTimes(1);
    });

    it('keeps the dialog and drafts after a failure, and retries from the dialog', async () => {
      vi.mocked(updateProviderDefinition)
        .mockRejectedValueOnce(new Error('write failed'))
        .mockResolvedValueOnce({ ...ACME, display_name: 'Acme Corp' });
      await openEditor();
      fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Acme Corp' } });
      save();
      clickInDialog('Confirm save');

      expect(await within(confirmDialog()).findByRole('alert')).toHaveTextContent('write failed');
      expect(screen.getByLabelText('Display name')).toHaveValue('Acme Corp');

      clickInDialog('Confirm save');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(updateProviderDefinition).toHaveBeenCalledTimes(2);
    });

    it('requires a probe for a new Base URL and never shows the probe key in the review', async () => {
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

      save();
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByRole('alert')).toHaveTextContent('Probe the new Base URL');

      fireEvent.click(screen.getByRole('button', { name: 'Probe' }));
      expect(await screen.findByText('Probe successful')).toBeInTheDocument();
      expect(screen.queryByRole('dialog')).toBeNull();

      save();
      const review = screen.getByRole('dialog', { name: 'Save 1 change to Acme?' });
      expect(review).toHaveTextContent(
        'Base URLhttps://api.acme.test/v1 → https://api2.acme.test/v1',
      );
      expect(review).toHaveTextContent('probed with model m-1');
      expect(review).not.toHaveTextContent(SECRET);
      clickInDialog('Confirm save');

      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
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
