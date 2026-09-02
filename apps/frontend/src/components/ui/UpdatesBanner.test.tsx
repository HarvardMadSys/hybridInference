// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';

const getPublicSiteUpdates = vi.fn();

vi.mock('@/lib/api/updates', () => ({
  getPublicSiteUpdates: () => getPublicSiteUpdates(),
}));

import { UpdatesBanner } from './UpdatesBanner';

describe('UpdatesBanner runtime storage namespace', () => {
  beforeEach(() => {
    localStorage.clear();
    getPublicSiteUpdates.mockResolvedValue({
      banner: {
        id: 'runtime-banner',
        title: 'Runtime announcement',
        body: '',
        link_url: '',
        link_label: '',
      },
      updates: [],
    });
  });

  afterEach(() => {
    cleanup();
    getPublicSiteUpdates.mockReset();
  });

  it('reads and writes the runtime prefix while keeping the configured namespace stable', async () => {
    const runtimeConfig = {
      ...buildTimeSiteConfig,
      branding: {
        ...buildTimeSiteConfig.branding,
        storageKeyPrefix: 'runtime-prefix',
      },
    };

    render(
      <SiteConfigProvider initialConfig={runtimeConfig}>
        <UpdatesBanner />
      </SiteConfigProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss announcement' }));

    expect(localStorage.getItem('runtime-prefix:dismissed-banner')).toBe('runtime-banner');
    expect(
      localStorage.getItem(`${buildTimeSiteConfig.branding.storageKeyPrefix}:dismissed-banner`),
    ).toBeNull();
  });
});
