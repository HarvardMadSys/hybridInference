import { describe, expect, it, vi } from 'vitest';

vi.mock('../../SettingsTab', () => ({ SettingsTab: () => null }));

import Page from './page';

function open(params: { tab?: string; missing?: string }) {
  return Page({ searchParams: Promise.resolve(params) });
}

describe('the Admin → Settings address', () => {
  it('opens on the tab and the filter a link names', async () => {
    const page = await open({ tab: 'alerts', missing: '1' });

    expect(page.props).toEqual({ initialTab: 'alerts', initialMissingOnly: true });
  });

  it('opens on the default tab for anything else', async () => {
    const page = await open({ tab: 'nonsense' });

    expect(page.props).toEqual({ initialTab: 'overview', initialMissingOnly: false });
  });
});
