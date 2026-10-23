// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('@/lib/api/admin', () => ({
  createSiteUpdate: vi.fn(),
  deleteSiteUpdate: vi.fn(),
  listSiteUpdates: vi.fn(),
  updateSiteUpdate: vi.fn(),
}));

import {
  createSiteUpdate,
  deleteSiteUpdate,
  listSiteUpdates,
  updateSiteUpdate,
} from '@/lib/api/admin';
import { SiteUpdatesTab } from './SiteUpdatesTab';

const DRAFT = {
  id: 'u1',
  title: 'Pricing note',
  body: 'Input tokens unchanged',
  placement: 'feed' as const,
  published: false,
  link_url: null,
  link_label: null,
  created_by: 'admin',
  created_at: '2026-10-22T10:00:00Z',
  updated_at: '2026-10-22T10:00:00Z',
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listSiteUpdates).mockResolvedValue({ total: 1, updates: [DRAFT] });
  vi.mocked(createSiteUpdate).mockResolvedValue(DRAFT);
  vi.mocked(updateSiteUpdate).mockResolvedValue(DRAFT);
  vi.stubGlobal('scrollTo', vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function renderTab() {
  render(<SiteUpdatesTab />);
  await screen.findByText('Pricing note');
}

describe('SiteUpdatesTab', () => {
  it('lists updates first, with the editor closed', async () => {
    await renderTab();

    expect(screen.queryByLabelText('Title')).not.toBeInTheDocument();
    expect(screen.getByText('Draft')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete Pricing note' })).toBeInTheDocument();
  });

  it('keeps a draft while the editor is hidden and clears it only on Cancel', async () => {
    await renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'New update' }));
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Half written' } });

    fireEvent.click(screen.getByRole('button', { name: 'Hide' }));
    expect(screen.queryByLabelText('Title')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume draft' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Resume draft' }));
    expect(screen.getByLabelText('Title')).toHaveValue('Half written');
    expect(screen.getByLabelText('Title')).toHaveFocus();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByLabelText('Title')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'New update' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'New update' }));
    expect(screen.getByLabelText('Title')).toHaveValue('');
  });

  it('creates an update with the same payload and closes the editor', async () => {
    await renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'New update' }));

    fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));
    expect(createSiteUpdate).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'New model' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));

    await waitFor(() =>
      expect(createSiteUpdate).toHaveBeenCalledWith({
        title: 'New model',
        body: '',
        placement: 'feed',
        published: true,
        link_url: null,
        link_label: null,
      }),
    );
    await waitFor(() => expect(screen.queryByLabelText('Title')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'New update' })).toHaveFocus();
  });

  it('edits in place and shows which update is being edited', async () => {
    await renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'Edit Pricing note' }));

    const editor = screen.getByRole('form', { name: 'Edit update' });
    expect(screen.getByLabelText('Title')).toHaveFocus();
    expect(within(editor).getByText('Editing “Pricing note”')).toBeInTheDocument();
    expect(screen.getByLabelText('Title')).toHaveValue('Pricing note');
    expect(screen.getByText('Editing')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Hide' }));
    expect(screen.getByRole('button', { name: 'Continue editing' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Continue editing' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() =>
      expect(updateSiteUpdate).toHaveBeenCalledWith('u1', {
        title: 'Pricing note',
        body: 'Input tokens unchanged',
        placement: 'feed',
        published: false,
        link_url: null,
        link_label: null,
      }),
    );
  });

  it('still asks before deleting', async () => {
    const confirm = vi.fn(() => false);
    vi.stubGlobal('confirm', confirm);
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Delete Pricing note' }));

    expect(confirm).toHaveBeenCalledWith('Delete "Pricing note"? This cannot be undone.');
    expect(deleteSiteUpdate).not.toHaveBeenCalled();
  });
});
