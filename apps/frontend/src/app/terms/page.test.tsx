// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import TermsPage from './page';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import { TERMS_SECTION_ANCHOR } from '@/site-ui/contract';

describe('TermsPage', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders exactly one page heading and the anchor the privacy link targets', () => {
    // The console's footer, the account pages and the sign-up consent step all
    // link to `/terms#terms-s5`. That anchor is part of the Site UI contract
    // (`TERMS_SECTION_ANCHOR`), because a module that invented its own prefix
    // would leave every one of those links pointing at nothing.
    render(
      <SiteConfigProvider
        initialConfig={{
          ...buildTimeSiteConfig,
          branding: { ...buildTimeSiteConfig.branding },
        }}
      >
        <TermsPage />
      </SiteConfigProvider>,
    );

    expect(document.getElementById(`${TERMS_SECTION_ANCHOR}5`)).not.toBeNull();
    expect(document.querySelectorAll('h1')).toHaveLength(1);
    expect(screen.getAllByText(/Last updated:/)).toHaveLength(1);
  });
});
