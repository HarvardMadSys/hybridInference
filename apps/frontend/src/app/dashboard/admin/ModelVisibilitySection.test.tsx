// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ModelVisibilitySection } from './ModelVisibilitySection';

vi.mock('@/lib/api/admin', () => ({
  listModelVisibility: vi.fn(),
  updateModelVisibility: vi.fn(),
}));

import { listModelVisibility, updateModelVisibility } from '@/lib/api/admin';

type InternalVisibilityUpdate = {
  model_id: string;
  baseline_required_role: 'free';
  override_required_role: 'internal';
  effective_required_role: 'internal';
};

describe('ModelVisibilitySection', () => {
  const onToast = vi.fn();

  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads and renders returned model visibility rows', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    });

    render(<ModelVisibilitySection onToast={onToast} />);

    expect(screen.getByText('Loading...')).toBeInTheDocument();
    expect(await screen.findByText('gpt-4o-mini')).toBeInTheDocument();
    expect(screen.getAllByText('free').length).toBeGreaterThanOrEqual(2);
  });

  it('PATCHes a role when the admin changes the select', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: 'internal',
      effective_required_role: 'internal',
    });

    render(<ModelVisibilitySection onToast={onToast} />);

    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
    });
    expect(onToast).toHaveBeenCalledWith('Updated visibility for gpt-4o-mini.');
  });

  it('sends null when Use default is selected', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: 'internal',
          effective_required_role: 'internal',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: null,
      effective_required_role: 'free',
    });

    render(<ModelVisibilitySection onToast={onToast} />);

    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: '__default__' } });

    await waitFor(() => {
      expect(updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', null);
    });
  });

  it('shows an inline error when loading fails', async () => {
    vi.mocked(listModelVisibility).mockRejectedValue(new Error('backend unavailable'));

    render(<ModelVisibilitySection onToast={onToast} />);

    expect(await screen.findByText('backend unavailable')).toBeInTheDocument();
  });

  it('retries loading after an inline error', async () => {
    vi.mocked(listModelVisibility)
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce({
        models: [
          {
            model_id: 'gpt-4o-mini',
            baseline_required_role: 'free',
            override_required_role: null,
            effective_required_role: 'free',
          },
        ],
      });

    render(<ModelVisibilitySection onToast={onToast} />);

    expect(await screen.findByText('backend unavailable')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('gpt-4o-mini')).toBeInTheDocument();
    await waitFor(() => {
      expect(listModelVisibility).toHaveBeenCalledTimes(2);
    });
  });

  it('disables only the saving row while an update is in flight', async () => {
    let resolveUpdate: undefined | ((value: InternalVisibilityUpdate) => void);

    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
        {
          model_id: 'claude-3-5-sonnet',
          baseline_required_role: 'pro',
          override_required_role: null,
          effective_required_role: 'pro',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpdate = resolve;
        }),
    );

    render(<ModelVisibilitySection onToast={onToast} />);

    const firstSelect = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    const secondSelect = screen.getByLabelText('Runtime override for claude-3-5-sonnet');

    fireEvent.change(firstSelect, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
    });
    expect(firstSelect).toBeDisabled();
    expect(secondSelect).not.toBeDisabled();

    if (!resolveUpdate) {
      throw new Error('Expected update promise resolver to be assigned');
    }

    resolveUpdate({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: 'internal',
      effective_required_role: 'internal',
    });

    await waitFor(() => {
      expect(firstSelect).not.toBeDisabled();
    });
  });

  it('shows a failure toast when an update fails', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockRejectedValue(new Error('write failed'));

    render(<ModelVisibilitySection onToast={onToast} />);

    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(onToast).toHaveBeenCalledWith('Failed to update gpt-4o-mini: write failed');
    });
  });
});
