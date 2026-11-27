import { afterEach, describe, expect, it, vi } from 'vitest';

// Next's redirect never returns: it throws, and the router follows it.
const redirectMock = vi.hoisted(() =>
  vi.fn((url: string) => {
    throw new Error(`NEXT_REDIRECT ${url}`);
  }),
);

vi.mock('next/navigation', () => ({ redirect: redirectMock }));
vi.mock('../../SettingsTab', () => ({ SettingsTab: () => null }));

import Page from './page';

function open(params: { tab?: string; missing?: string }) {
  return Page({ searchParams: Promise.resolve(params) });
}

afterEach(() => {
  redirectMock.mockClear();
});

describe('the Admin → Settings address', () => {
  it('opens on the sub-tab and the filter a link names', async () => {
    const page = await open({ tab: 'alerts', missing: '1' });

    expect(page.props).toEqual({ initialTab: 'alerts', initialMissingOnly: true });
    expect(redirectMock).not.toHaveBeenCalled();
  });

  it('sends a link to the old Models sub-tab to Admin → Models', async () => {
    await expect(open({ tab: 'models' })).rejects.toThrow('NEXT_REDIRECT /dashboard/admin/models');
  });

  it('keeps an old link to the missing settings here, on Application', async () => {
    const page = await open({ tab: 'models', missing: '1' });

    expect(redirectMock).not.toHaveBeenCalled();
    expect(page.props).toEqual({ initialTab: 'application', initialMissingOnly: true });
  });
});
