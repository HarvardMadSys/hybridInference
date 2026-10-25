// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/admin', () => ({
  getUsageInsightsSettings: vi.fn(),
  updateUsageInsightsSettings: vi.fn(),
}));

import { getUsageInsightsSettings, updateUsageInsightsSettings } from '@/lib/api/admin';

import { UsageInsightsSettingsSection } from './UsageInsightsSettingsSection';

const TYPED_KEY = 'sk-typed-new-key-never-shown';
const SAVED = {
  configured: true,
  api_key_hint: 'sk-…abcd',
  model: 'glm-5.1',
} as Awaited<ReturnType<typeof getUsageInsightsSettings>>;

beforeEach(() => {
  vi.mocked(getUsageInsightsSettings).mockResolvedValue(SAVED);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function openEditor(onToast = vi.fn()) {
  render(<UsageInsightsSettingsSection onToast={onToast} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit Usage Insights' }));
  return onToast;
}

describe('UsageInsightsSettingsSection', () => {
  it('shows the saved model as text and no key field until Edit', async () => {
    render(<UsageInsightsSettingsSection onToast={vi.fn()} />);
    expect(await screen.findByText('glm-5.1')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByLabelText('Gateway API key')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Edit Usage Insights' }));
    expect(screen.getByLabelText('Model')).toHaveValue('glm-5.1');
    expect(screen.getByLabelText('Gateway API key')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Save changes to Usage Insights' })).toBeDisabled();
  });

  it('Cancel discards the drafts and writes nothing', async () => {
    await openEditor();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-6' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Usage Insights' }));
    expect(screen.getByText('glm-5.1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Edit Usage Insights' }));
    expect(screen.getByLabelText('Model')).toHaveValue('glm-5.1');
    expect(updateUsageInsightsSettings).not.toHaveBeenCalled();
  });

  it('confirms old -> new without the key, keeps drafts on Back, and saves once', async () => {
    vi.mocked(updateUsageInsightsSettings).mockResolvedValue({ ...SAVED, model: 'glm-6' });
    const onToast = await openEditor();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-6' } });
    fireEvent.change(screen.getByLabelText('Gateway API key'), { target: { value: TYPED_KEY } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));

    const dialog = screen.getByRole('dialog', { name: 'Save 2 changes to Usage Insights?' });
    expect(dialog).toHaveTextContent('Modelglm-5.1 → glm-6');
    expect(dialog).toHaveTextContent('Gateway API keySet → Replace');
    expect(document.body.textContent).not.toContain(TYPED_KEY);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to editing' }));
    expect(screen.getByLabelText('Model')).toHaveValue('glm-6');
    expect(updateUsageInsightsSettings).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(updateUsageInsightsSettings).toHaveBeenCalledTimes(1);
    expect(updateUsageInsightsSettings).toHaveBeenCalledWith({
      api_key: TYPED_KEY,
      model: 'glm-6',
    });
    expect(onToast).toHaveBeenCalledWith('Usage Insights provider saved');
    expect(screen.queryByLabelText('Gateway API key')).toBeNull();
  });

  it('keeps the dialog and the drafts when the save fails', async () => {
    vi.mocked(updateUsageInsightsSettings).mockRejectedValueOnce(new Error('upstream down'));
    await openEditor();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-6' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm save' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('upstream down');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to editing' }));
    expect(screen.getByLabelText('Model')).toHaveValue('glm-6');
  });

  it('masks the typed key in a failed save, and keeps it for the retry', async () => {
    vi.mocked(updateUsageInsightsSettings)
      .mockRejectedValueOnce(new Error(`api_key ${TYPED_KEY} was rejected`))
      .mockResolvedValueOnce(SAVED);
    await openEditor();
    fireEvent.change(screen.getByLabelText('Gateway API key'), {
      target: { value: ` ${TYPED_KEY} ` },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm save' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('api_key •••• was rejected');
    expect(document.body.textContent).not.toContain(TYPED_KEY);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm save' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(updateUsageInsightsSettings).mock.calls).toEqual([
      [{ api_key: TYPED_KEY }],
      [{ api_key: TYPED_KEY }],
    ]);
  });

  it('refuses an empty model without asking or writing', async () => {
    await openEditor();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: '  ' } });
    fireEvent.change(screen.getByLabelText('Gateway API key'), { target: { value: TYPED_KEY } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent('Model name cannot be empty.');
  });

  it('clears the key only after a confirmation', async () => {
    vi.mocked(updateUsageInsightsSettings).mockResolvedValue({
      ...SAVED,
      configured: false,
      api_key_hint: null,
    } as unknown as typeof SAVED);
    render(<UsageInsightsSettingsSection onToast={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Clear key' }));
    const dialog = screen.getByRole('dialog', { name: 'Clear the Usage Insights API key?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(updateUsageInsightsSettings).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Clear key' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clear key' }));
    await waitFor(() => expect(updateUsageInsightsSettings).toHaveBeenCalledWith({ api_key: '' }));
    expect(updateUsageInsightsSettings).toHaveBeenCalledTimes(1);
  });
});
