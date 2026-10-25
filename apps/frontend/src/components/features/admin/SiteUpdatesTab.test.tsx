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

import toast from 'react-hot-toast';
import {
  createSiteUpdate,
  deleteSiteUpdate,
  listSiteUpdates,
  updateSiteUpdate,
  type SiteUpdateItem,
} from '@/lib/api/admin';
import { chooseOption, openOptions } from '@/test-utils/select';
import { SiteUpdatesTab, plainExcerpt } from './SiteUpdatesTab';
import { uncontainedGrids } from './__tests__/layoutContainment';

/** An unpublished feed entry, alone in the list. */
const PRICING_NOTE: SiteUpdateItem = {
  id: 'u1',
  title: 'Pricing note',
  body: 'Input tokens unchanged',
  placement: 'feed',
  published: false,
  link_url: null,
  link_label: null,
  created_by: 'admin',
  created_at: '2026-10-22T10:00:00Z',
  updated_at: '2026-10-22T10:00:00Z',
};

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

let nativeConfirm: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listSiteUpdates).mockResolvedValue({ total: 2, updates: [PUBLISHED, DRAFT] });
  vi.mocked(createSiteUpdate).mockResolvedValue(PUBLISHED);
  vi.mocked(updateSiteUpdate).mockResolvedValue(PUBLISHED);
  // No prompt in this tab is a native one: a call here is a regression.
  nativeConfirm = vi.fn(() => true);
  vi.stubGlobal('confirm', nativeConfirm);
  vi.stubGlobal('scrollTo', vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function dialog() {
  return screen.getByRole('dialog');
}

function clickInDialog(name: string) {
  fireEvent.click(within(dialog()).getByRole('button', { name }));
}

function expectNoWrites() {
  expect(createSiteUpdate).not.toHaveBeenCalled();
  expect(updateSiteUpdate).not.toHaveBeenCalled();
  expect(deleteSiteUpdate).not.toHaveBeenCalled();
}

/** The list holding only PRICING_NOTE (or `updates`). */
async function renderPricingNote(updates: SiteUpdateItem[] = [PRICING_NOTE]) {
  vi.mocked(listSiteUpdates).mockResolvedValue({ total: updates.length, updates });
  render(<SiteUpdatesTab />);
  await screen.findByText(updates[0].title);
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
  it('lists updates first, with the editor closed', async () => {
    await renderPricingNote();

    expect(screen.queryByLabelText('Title')).not.toBeInTheDocument();
    expect(screen.getByText('Draft')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete Pricing note' })).toBeInTheDocument();
  });

  it('lists updates read-only, with no form until New update or Edit', async () => {
    await renderTab();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Create Update' })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'New update' })).toBeVisible();
  });

  it('keeps a draft while the editor is hidden and clears it only on Cancel', async () => {
    await renderPricingNote();
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
    expectNoWrites();
  });

  it('creates an update with the same payload after review and closes the editor', async () => {
    await renderPricingNote();
    fireEvent.click(screen.getByRole('button', { name: 'New update' }));

    fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));
    expect(createSiteUpdate).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'New model' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));
    expect(createSiteUpdate).not.toHaveBeenCalled();
    clickInDialog('Create and publish');

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
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(createSiteUpdate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'New update' })).toHaveFocus();
  });

  describe('keyboard', () => {
    it('saves only from the save button, not when Enter submits the form', async () => {
      await renderPricingNote();
      fireEvent.click(screen.getByRole('button', { name: 'New update' }));
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Half written' } });
      const composer = screen.getByRole('form', { name: 'New update' });

      fireEvent.submit(composer);
      expect(createSiteUpdate).not.toHaveBeenCalled();
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByLabelText('Title')).toHaveValue('Half written');
      // Enter in a field clicks the form's first submit button instead, when it
      // has one, so the save button must not be one.
      expect(composer.querySelector('[type="submit"], button:not([type])')).toBeNull();

      fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));
      expect(createSiteUpdate).not.toHaveBeenCalled();
      clickInDialog('Create and publish');
      await waitFor(() => expect(createSiteUpdate).toHaveBeenCalledTimes(1));
    });

    it('opens no review and writes nothing when Enter submits an edit', async () => {
      await openEditor();
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Llama 4 is here' } });
      const composer = screen.getByRole('form', { name: 'Edit update' });

      fireEvent.keyDown(screen.getByLabelText('Title'), { key: 'Enter' });
      fireEvent.submit(composer);
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(composer.querySelector('[type="submit"], button:not([type])')).toBeNull();
      expect(screen.getByLabelText('Title')).toHaveValue('Llama 4 is here');
      expectNoWrites();
    });
  });

  it('edits in place and shows which update is being edited', async () => {
    await renderPricingNote();
    fireEvent.click(screen.getByRole('button', { name: 'Edit Pricing note' }));

    const editor = screen.getByRole('form', { name: 'Edit update' });
    expect(screen.getByLabelText('Title')).toHaveFocus();
    expect(within(editor).getByText('Editing “Pricing note”')).toBeInTheDocument();
    expect(screen.getByLabelText('Title')).toHaveValue('Pricing note');
    expect(screen.getByText('Editing')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Hide' }));
    expect(screen.getByRole('button', { name: 'Continue editing' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Continue editing' }));
    fireEvent.change(screen.getByLabelText('Title'), {
      target: { value: 'Pricing note, revised' },
    });
    saveEdit('Pricing note');
    clickInDialog('Confirm save');
    await waitFor(() =>
      expect(vi.mocked(updateSiteUpdate).mock.calls).toEqual([
        ['u1', { title: 'Pricing note, revised' }],
      ]),
    );
  });

  describe('drafts when another update is opened for editing', () => {
    it('asks before Edit replaces a hidden draft, and keeps the draft if told no', async () => {
      await renderPricingNote();
      fireEvent.click(screen.getByRole('button', { name: 'New update' }));
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Half written' } });
      fireEvent.click(screen.getByRole('button', { name: 'Hide' }));

      fireEvent.click(screen.getByRole('button', { name: 'Edit Pricing note' }));
      const prompt = screen.getByRole('dialog', { name: 'Edit “Pricing note”?' });
      expect(prompt).toHaveTextContent('Opening it discards your unsaved new update.');
      expect(nativeConfirm).not.toHaveBeenCalled();
      clickInDialog('Keep my changes');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.queryByLabelText('Title')).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Resume draft' }));
      expect(screen.getByLabelText('Title')).toHaveValue('Half written');

      fireEvent.click(screen.getByRole('button', { name: 'Edit Pricing note' }));
      clickInDialog('Discard and edit');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(screen.getByLabelText('Title')).toHaveValue('Pricing note');
      expect(screen.getByLabelText('Title')).toHaveFocus();
      expectNoWrites();
    });

    it('asks before Edit on another update replaces unsaved changes, not to resume', async () => {
      await renderPricingNote([PRICING_NOTE, { ...PRICING_NOTE, id: 'u2', title: 'Banner' }]);
      fireEvent.click(screen.getByRole('button', { name: 'Edit Pricing note' }));
      // Nothing changed yet: nothing to ask about.
      fireEvent.click(screen.getByRole('button', { name: 'Edit Banner' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByLabelText('Title')).toHaveValue('Banner');

      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Banner, revised' } });
      fireEvent.click(screen.getByRole('button', { name: 'Edit Pricing note' }));
      expect(screen.getByRole('dialog', { name: 'Edit “Pricing note”?' })).toHaveTextContent(
        'Opening it discards your unsaved changes to “Banner”.',
      );
      clickInDialog('Keep my changes');
      expect(screen.getByLabelText('Title')).toHaveValue('Banner, revised');

      // Edit on the update being edited picks its changes up again.
      fireEvent.click(screen.getByRole('button', { name: 'Hide' }));
      fireEvent.click(screen.getByRole('button', { name: 'Edit Banner' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByLabelText('Title')).toHaveValue('Banner, revised');
      expect(nativeConfirm).not.toHaveBeenCalled();
      expectNoWrites();
    });

    it('discarding to edit another update writes nothing and starts from its saved values', async () => {
      await openEditor();
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Unsaved' } });
      fireEvent.click(screen.getByRole('button', { name: 'Edit Maintenance window' }));
      clickInDialog('Discard and edit');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

      expect(screen.getByLabelText('Title')).toHaveValue('Maintenance window');
      expect(screen.getByText('No changes yet')).toBeVisible();
      expect(
        screen.getByRole('button', { name: 'Save changes to Maintenance window' }),
      ).toBeDisabled();
      // The first update's Publish and Delete are free again; the new one's are locked.
      expect(screen.getByRole('button', { name: 'Unpublish Llama 4 available' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Publish Maintenance window' })).toBeDisabled();
      expectNoWrites();
    });
  });

  it('asks before deleting, in the shared dialog', async () => {
    await renderPricingNote();

    fireEvent.click(screen.getByRole('button', { name: 'Delete Pricing note' }));

    expect(screen.getByRole('dialog', { name: 'Delete “Pricing note”?' })).toHaveTextContent(
      'This cannot be undone.',
    );
    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(deleteSiteUpdate).not.toHaveBeenCalled();
  });

  it('previews the body as plain text and shows the full Markdown on request', async () => {
    await renderPricingNote([
      {
        ...PRICING_NOTE,
        body: '- **Input** tokens unchanged\n- Output tokens [unchanged](https://x.test)',
        link_url: 'https://docs.example.com/pricing',
        link_label: 'Read more',
      },
    ]);

    expect(
      screen.getByText('Input tokens unchanged · Output tokens unchanged'),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Read more (docs.example.com)' })).toHaveAttribute(
      'href',
      'https://docs.example.com/pricing',
    );
    // The list shows one row; the body's own list is not rendered yet.
    expect(screen.getAllByRole('listitem')).toHaveLength(1);

    const toggle = screen.getByRole('button', { name: 'Show full text of Pricing note' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    const body = document.getElementById(toggle.getAttribute('aria-controls')!)!;
    expect(within(body).getAllByRole('listitem')).toHaveLength(2);
    expect(within(body).getByRole('link', { name: 'unchanged' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Hide full text of Pricing note' }));
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
  });

  it('keeps a long title and a wide table as they are, inside the panel', async () => {
    const title = 'A'.repeat(180);
    const table = `| a | b |\n|---|---|\n| ${'x'.repeat(300)} | y |`;
    vi.mocked(listSiteUpdates).mockResolvedValue({
      total: 1,
      updates: [{ ...PRICING_NOTE, title, body: table }],
    });
    render(<SiteUpdatesTab />);

    const heading = await screen.findByRole('heading', { name: title });
    expect(heading.className).toContain('[overflow-wrap:anywhere]');
    fireEvent.click(screen.getByRole('button', { name: `Show full text of ${title}` }));
    const rendered = screen.getByRole('table');
    // Wide content scrolls inside the body box instead of widening the page.
    expect(rendered.closest('div')!.className).toContain('[&_table]:overflow-x-auto');
    expect(within(rendered).getByText('x'.repeat(300))).toBeInTheDocument();
  });

  it('shows the entry being written in a preview beside the fields', async () => {
    await renderPricingNote();
    fireEvent.click(screen.getByRole('button', { name: 'New update' }));
    const preview = screen.getByRole('region', { name: 'Preview' });
    expect(preview).toHaveTextContent('The title and body appear here as you type.');

    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Maintenance' } });
    fireEvent.change(screen.getByLabelText('Body (Markdown)'), { target: { value: '**Soon**' } });
    fireEvent.change(screen.getByLabelText('Link URL (optional)'), {
      target: { value: 'https://status.example.com' },
    });

    expect(within(preview).getByText('Maintenance')).toBeInTheDocument();
    expect(within(preview).getByText('Soon').tagName).toBe('STRONG');
    expect(within(preview).getByText('Learn more')).toBeInTheDocument();
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
      chooseOption('Placement', 'Banner (top notice)');
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

    it('browsing or choosing the same placement again is no change and writes nothing', async () => {
      await openEditor();
      const placement = screen.getByRole('combobox', { name: 'Placement' });
      openOptions(placement);
      fireEvent.keyDown(placement, { key: 'Escape' });
      chooseOption('Placement', 'Feed (Updates section)');
      expect(screen.getByText('No changes yet')).toBeVisible();
      expect(
        screen.getByRole('button', { name: 'Save changes to Llama 4 available' }),
      ).toBeDisabled();

      chooseOption('Placement', 'Banner (top notice)');
      expect(screen.getByText('1 unsaved change')).toBeVisible();
      expect(screen.queryByRole('dialog')).toBeNull();
      expectNoWrites();
    });

    it('Cancel discards drafts and writes nothing', async () => {
      await openEditor();
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Changed' } });
      fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Llama 4 available' }));
      expect(screen.queryByLabelText('Title')).toBeNull();
      expect(screen.getByText('Llama 4 available')).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Edit Llama 4 available' }));
      expect(screen.queryByRole('dialog')).toBeNull();
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
      expect(screen.getByRole('combobox', { name: 'Placement' })).toBeDisabled();
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

    it('locks the edited row’s discrete actions while drafts are open, hidden or not', async () => {
      await openEditor();
      expect(screen.getByRole('button', { name: 'Unpublish Llama 4 available' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Delete Llama 4 available' })).toBeDisabled();
      // Another update can still be opened for editing; that asks before
      // discarding these drafts (covered above).
      expect(screen.getByRole('button', { name: 'Edit Maintenance window' })).toBeEnabled();
      expect(screen.queryByRole('button', { name: 'New update' })).toBeNull();

      fireEvent.click(screen.getByRole('button', { name: 'Hide' }));
      expect(screen.getByRole('button', { name: 'Unpublish Llama 4 available' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Delete Llama 4 available' })).toBeDisabled();
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

describe('SiteUpdatesTab composer on a narrow screen', () => {
  it('keeps a long title and a wide table in the preview inside the composer', async () => {
    await renderPricingNote();
    fireEvent.click(screen.getByRole('button', { name: 'New update' }));
    const title = 'LongTitle'.repeat(40);
    const cells = Array.from({ length: 6 }, () => 'X'.repeat(80));
    const table = `| ${cells.map((_, i) => `c${i}`).join(' | ')} |\n|${'---|'.repeat(6)}\n| ${cells.join(' | ')} |`;
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: title } });
    fireEvent.change(screen.getByLabelText('Body (Markdown)'), { target: { value: table } });

    const composer = screen.getByRole('form', { name: 'New update' });
    const preview = within(composer).getByRole('region', { name: 'Preview' });
    const rendered = within(preview).getByRole('table');
    // The strings are untouched…
    expect(within(preview).getByText(title)).toBeInTheDocument();
    expect(within(rendered).getAllByText('X'.repeat(80))).toHaveLength(6);
    // …and nothing between them and the composer can grow past its width.
    expect(uncontainedGrids(rendered, composer)).toEqual([]);
    expect(uncontainedGrids(screen.getByLabelText('Title'), composer)).toEqual([]);
  });
});

describe('plainExcerpt', () => {
  it('drops Markdown marks but keeps the words', () => {
    expect(plainExcerpt('# Title\n\nSee the [docs](https://x.test) for `limits`.')).toBe(
      'Title See the docs for limits.',
    );
    expect(plainExcerpt('1. one\n2. two\n\n> quoted')).toBe('one · two quoted');
  });
});
