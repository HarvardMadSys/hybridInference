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
import { SiteUpdatesTab, plainExcerpt } from './SiteUpdatesTab';
import { uncontainedGrids } from './__tests__/layoutContainment';

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

  it('saves only from the save button, not when Enter submits the form', async () => {
    await renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'New update' }));
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Half written' } });
    const composer = screen.getByRole('form', { name: 'New update' });

    fireEvent.submit(composer);
    expect(createSiteUpdate).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Title')).toHaveValue('Half written');
    // Enter in a field clicks the form's first submit button instead, when it
    // has one, so the save button must not be one.
    expect(composer.querySelector('[type="submit"], button:not([type])')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Create Update' }));
    await waitFor(() => expect(createSiteUpdate).toHaveBeenCalledTimes(1));
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

  it('previews the body as plain text and shows the full Markdown on request', async () => {
    vi.mocked(listSiteUpdates).mockResolvedValue({
      total: 1,
      updates: [
        {
          ...DRAFT,
          body: '- **Input** tokens unchanged\n- Output tokens [unchanged](https://x.test)',
          link_url: 'https://docs.example.com/pricing',
          link_label: 'Read more',
        },
      ],
    });
    await renderTab();

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
      updates: [{ ...DRAFT, title, body: table }],
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
    await renderTab();
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
});

describe('SiteUpdatesTab composer on a narrow screen', () => {
  it('keeps a long title and a wide table in the preview inside the composer', async () => {
    await renderTab();
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
