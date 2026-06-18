// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Updates } from './Updates';

const getPublicSiteUpdates = vi.fn();

vi.mock('@/lib/api/updates', () => ({
  getPublicSiteUpdates: () => getPublicSiteUpdates(),
}));

describe('Updates', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    getPublicSiteUpdates.mockReset();
  });

  it('renders published feed entries newest-first as returned', async () => {
    getPublicSiteUpdates.mockResolvedValue({
      banner: null,
      updates: [
        {
          id: '1',
          title: 'Llama 4 is live',
          body: 'Try it **now**.',
          link_url: 'https://example.com',
          link_label: 'Docs',
          created_at: '2026-06-01T00:00:00Z',
        },
      ],
    });

    render(<Updates />);

    expect(await screen.findByText('Llama 4 is live')).toBeInTheDocument();
    expect(screen.getByText('Latest Updates')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Docs' })).toHaveAttribute(
      'href',
      'https://example.com',
    );
  });

  it('renders nothing when there are no updates', async () => {
    getPublicSiteUpdates.mockResolvedValue({ banner: null, updates: [] });

    const { container } = render(<Updates />);

    await waitFor(() => expect(getPublicSiteUpdates).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
