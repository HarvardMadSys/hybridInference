// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/admin', () => ({
  listSiteUpdates: vi.fn(),
  createSiteUpdate: vi.fn(),
  updateSiteUpdate: vi.fn(),
  deleteSiteUpdate: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import toast from 'react-hot-toast';
import {
  createSiteUpdate,
  deleteSiteUpdate,
  listSiteUpdates,
  updateSiteUpdate,
  type SiteUpdateItem,
} from '@/lib/api/admin';

import { SiteUpdatesTab } from './SiteUpdatesTab';

const PUBLISHED: SiteUpdateItem = {
  id: 'u1',
  title: 'Llama 4 available',
  body: 'Try it **now**.',
  placement: 'feed',
  published: true,
  link_url: null,
  link_label: null,
  created_by: 'admin',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

const DRAFT: SiteUpdateItem = {
  ...PUBLISHED,
  id: 'u2',
  title: 'Maintenance window',
  body: '',
  placement: 'banner',
  published: false,
};

function dialog() {
  return screen.getByRole('dialog');
}

function clickInDialog(name: string) {
  fireEvent.click(within(dialog()).getByRole('button', { name }));
}

async function renderTab() {
  render(<SiteUpdatesTab />);
  await screen.findByText('Llama 4 available');
}

async function openEditor(title = 'Llama 4 available') {
  await renderTab();
  fireEvent.click(screen.getByRole('button', { name: `Edit ${title}` }));
}

function saveEdit(title = 'Llama 4 available') {
  fireEvent.click(screen.getByRole('button', { name: `Save changes to ${title}` }));
}

describe('SiteUpdatesTab', () => {
  let nativeConfirm: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listSiteUpdates).mockResolvedValue({ total: 2, updates: [PUBLISHED, DRAFT] });
    nativeConfirm = vi.fn(() => true);
    vi.stubGlobal('confirm', nativeConfirm);
    window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('lists updates read-only, with no form until New update or Edit', async () => {
    await renderTab();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Create Update' })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'New update' })).toBeVisible();
  });

  describe('create', () => {
    it('keeps the title requirement and writes nothing for an empty title', async () => {
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'New update' }));
      fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));
      expect(toast.error).toHaveBeenCalledWith('Title is required');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(createSiteUpdate).not.toHaveBeenCalled();
    });

    it('Cancel closes the form with zero writes', async () => {
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'New update' }));
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Draft idea' } });
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(screen.queryByLabelText('Title')).toBeNull();
      expect(createSiteUpdate).not.toHaveBeenCalled();
    });

    it('reviews what will be published, keeps the form on Back, and creates once on Confirm', async () => {
      vi.mocked(createSiteUpdate).mockResolvedValue({ ...PUBLISHED, id: 'u3' });
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'New update' }));
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'GPT-5 support' } });
      fireEvent.change(screen.getByLabelText('Placement'), { target: { value: 'banner' } });
      fireEvent.change(screen.getByLabelText('Body (Markdown)'), {
        target: { value: 'Now live.' },
      });
      fireEvent.change(screen.getByLabelText('Link URL (optional)'), {
        target: { value: 'https://docs.example.com/gpt-5' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));

      const review = screen.getByRole('dialog', { name: 'Create and publish this update?' });
      expect(review).toHaveTextContent('TitleGPT-5 support');
      expect(review).toHaveTextContent('PlacementBanner');
      expect(review).toHaveTextContent('BodyNow live.');
      expect(review).toHaveTextContent('Link URLhttps://docs.example.com/gpt-5');
      expect(review).toHaveTextContent('Link label(empty)');
      expect(review).toHaveTextContent('VisibilityPublished');
      expect(review).toHaveTextContent('It appears as the homepage banner');
      expect(createSiteUpdate).not.toHaveBeenCalled();

      clickInDialog('Back to editing');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByLabelText('Title')).toHaveValue('GPT-5 support');
      expect(createSiteUpdate).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));
      clickInDialog('Create and publish');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(createSiteUpdate).mock.calls).toEqual([
        [
          {
            title: 'GPT-5 support',
            body: 'Now live.',
            placement: 'banner',
            published: true,
            link_url: 'https://docs.example.com/gpt-5',
            link_label: null,
          },
        ],
      ]);
      expect(toast.success).toHaveBeenCalledWith('Update created');
      expect(screen.queryByLabelText('Title')).toBeNull();
      expect(listSiteUpdates).toHaveBeenCalledTimes(2);
    });

    it('names an unpublished create as a draft', async () => {
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'New update' }));
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Later' } });
      fireEvent.click(screen.getByLabelText('Published (visible on homepage)'));
      fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));
      const review = screen.getByRole('dialog', { name: 'Create this update as a draft?' });
      expect(review).toHaveTextContent('VisibilityDraft');
      expect(within(review).getByRole('button', { name: 'Create draft' })).toBeVisible();
    });

    it('keeps the review open with the error when the create fails, and retries', async () => {
      vi.mocked(createSiteUpdate)
        .mockRejectedValueOnce(new Error('link_url must be https'))
        .mockResolvedValueOnce({ ...PUBLISHED, id: 'u3' });
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'New update' }));
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Retry me' } });
      fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));
      clickInDialog('Create and publish');

      expect(await within(dialog()).findByRole('alert')).toHaveTextContent(
        'link_url must be https',
      );
      expect(screen.getByLabelText('Title')).toHaveValue('Retry me');
      clickInDialog('Create and publish');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(createSiteUpdate).toHaveBeenCalledTimes(2);
    });
  });

  describe('edit', () => {
    it('opens drafts from the saved update; a value typed back is no change', async () => {
      await openEditor();
      expect(screen.getByLabelText('Title')).toHaveValue('Llama 4 available');
      expect(screen.getByText('No changes yet')).toBeVisible();
      const save = screen.getByRole('button', { name: 'Save changes to Llama 4 available' });
      expect(save).toBeDisabled();

      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Llama 4 is here' } });
      expect(screen.getByText('1 unsaved change')).toBeVisible();
      expect(save).toBeEnabled();
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Llama 4 available' } });
      expect(save).toBeDisabled();
      expect(updateSiteUpdate).not.toHaveBeenCalled();
    });

    it('Cancel discards drafts and writes nothing', async () => {
      await openEditor();
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Changed' } });
      fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Llama 4 available' }));
      expect(screen.queryByLabelText('Title')).toBeNull();
      expect(screen.getByText('Llama 4 available')).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Edit Llama 4 available' }));
      expect(screen.getByLabelText('Title')).toHaveValue('Llama 4 available');
      expect(updateSiteUpdate).not.toHaveBeenCalled();
    });

    it('lists old -> new, keeps drafts on Back, and writes only the changed fields once', async () => {
      vi.mocked(updateSiteUpdate).mockResolvedValue({ ...PUBLISHED, title: 'Llama 4 is here' });
      await openEditor();
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Llama 4 is here' } });
      fireEvent.change(screen.getByLabelText('Link URL (optional)'), {
        target: { value: 'https://docs.example.com/llama' },
      });
      expect(screen.getByText('2 unsaved changes')).toBeVisible();
      saveEdit();

      const review = screen.getByRole('dialog', {
        name: 'Save 2 changes to “Llama 4 available”?',
      });
      expect(review).toHaveTextContent('TitleLlama 4 available → Llama 4 is here');
      expect(review).toHaveTextContent('Link URL(empty) → https://docs.example.com/llama');
      expect(review).not.toHaveTextContent('Placement');
      expect(updateSiteUpdate).not.toHaveBeenCalled();

      clickInDialog('Back to editing');
      expect(screen.getByLabelText('Title')).toHaveValue('Llama 4 is here');
      expect(updateSiteUpdate).not.toHaveBeenCalled();

      saveEdit();
      clickInDialog('Confirm save');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(updateSiteUpdate).mock.calls).toEqual([
        ['u1', { title: 'Llama 4 is here', link_url: 'https://docs.example.com/llama' }],
      ]);
      expect(toast.success).toHaveBeenCalledWith('Update saved');
      expect(screen.queryByLabelText('Title')).toBeNull();
    });

    it('blocks a second Confirm and closing while the save is in flight', async () => {
      let finish!: (value: SiteUpdateItem) => void;
      vi.mocked(updateSiteUpdate).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      await openEditor();
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Busy' } });
      saveEdit();
      clickInDialog('Confirm save');

      expect(within(dialog()).getByRole('button', { name: 'Saving…' })).toBeDisabled();
      expect(within(dialog()).getByRole('button', { name: 'Back to editing' })).toBeDisabled();
      expect(screen.getByLabelText('Title')).toBeDisabled();
      fireEvent.click(within(dialog()).getByRole('button', { name: 'Saving…' }));
      fireEvent.keyDown(window, { key: 'Escape' });
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(updateSiteUpdate).toHaveBeenCalledTimes(1);

      finish({ ...PUBLISHED, title: 'Busy' });
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(updateSiteUpdate).toHaveBeenCalledTimes(1);
    });

    it('keeps the dialog and drafts after a failure, and retries from the dialog', async () => {
      vi.mocked(updateSiteUpdate)
        .mockRejectedValueOnce(new Error('write failed'))
        .mockResolvedValueOnce({ ...PUBLISHED, title: 'Retry' });
      await openEditor();
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Retry' } });
      saveEdit();
      clickInDialog('Confirm save');

      expect(await within(dialog()).findByRole('alert')).toHaveTextContent('write failed');
      expect(screen.getByLabelText('Title')).toHaveValue('Retry');
      clickInDialog('Confirm save');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(updateSiteUpdate).mock.calls).toEqual([
        ['u1', { title: 'Retry' }],
        ['u1', { title: 'Retry' }],
      ]);
    });

    it('locks the edited row’s discrete actions and other Edits while drafts are open', async () => {
      await openEditor();
      expect(screen.getByRole('button', { name: 'Unpublish Llama 4 available' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Delete Llama 4 available' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Edit Maintenance window' })).toBeDisabled();
      expect(screen.queryByRole('button', { name: 'New update' })).toBeNull();
    });
  });

  describe('publish / unpublish', () => {
    it('confirms an unpublish naming the update; Cancel writes nothing, Confirm writes once', async () => {
      vi.mocked(updateSiteUpdate).mockResolvedValue({ ...PUBLISHED, published: false });
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'Unpublish Llama 4 available' }));
      const review = screen.getByRole('dialog', { name: 'Unpublish “Llama 4 available”?' });
      expect(review).toHaveTextContent('disappears from the homepage');
      clickInDialog('Cancel');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(updateSiteUpdate).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Unpublish Llama 4 available' }));
      clickInDialog('Unpublish');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(updateSiteUpdate).mock.calls).toEqual([['u1', { published: false }]]);
      expect(listSiteUpdates).toHaveBeenCalledTimes(2);
    });

    it('confirms a publish with where it appears', async () => {
      vi.mocked(updateSiteUpdate).mockResolvedValue({ ...DRAFT, published: true });
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'Publish Maintenance window' }));
      const review = screen.getByRole('dialog', { name: 'Publish “Maintenance window”?' });
      expect(review).toHaveTextContent('It appears as the homepage banner right away.');
      clickInDialog('Publish');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(updateSiteUpdate).mock.calls).toEqual([['u2', { published: true }]]);
    });

    it('keeps the dialog with the error when the toggle fails', async () => {
      vi.mocked(updateSiteUpdate).mockRejectedValueOnce(new Error('toggle failed'));
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'Publish Maintenance window' }));
      clickInDialog('Publish');
      expect(await within(dialog()).findByRole('alert')).toHaveTextContent('toggle failed');
      expect(updateSiteUpdate).toHaveBeenCalledTimes(1);
    });
  });

  describe('delete', () => {
    it('uses the shared danger dialog instead of window.confirm', async () => {
      vi.mocked(deleteSiteUpdate).mockResolvedValue(undefined);
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'Delete Llama 4 available' }));
      const review = screen.getByRole('dialog', { name: 'Delete “Llama 4 available”?' });
      expect(review).toHaveTextContent('This cannot be undone.');
      expect(nativeConfirm).not.toHaveBeenCalled();
      expect(within(review).getByRole('button', { name: 'Cancel' })).toHaveFocus();

      clickInDialog('Cancel');
      expect(deleteSiteUpdate).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Delete Llama 4 available' }));
      clickInDialog('Delete');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(deleteSiteUpdate).mock.calls).toEqual([['u1']]);
      expect(toast.success).toHaveBeenCalledWith('Update deleted');
    });

    it('keeps the dialog with the error when the delete fails, and retries', async () => {
      vi.mocked(deleteSiteUpdate)
        .mockRejectedValueOnce(new Error('delete failed'))
        .mockResolvedValueOnce(undefined);
      await renderTab();
      fireEvent.click(screen.getByRole('button', { name: 'Delete Maintenance window' }));
      clickInDialog('Delete');
      expect(await within(dialog()).findByRole('alert')).toHaveTextContent('delete failed');
      clickInDialog('Delete');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(deleteSiteUpdate).mock.calls).toEqual([['u2'], ['u2']]);
    });
  });
});
