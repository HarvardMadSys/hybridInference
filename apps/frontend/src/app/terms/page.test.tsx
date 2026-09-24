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

  it('renders legal-style terms for service use, logging, and disclaimers', () => {
    render(<TermsPage />);

    expect(
      screen.getByRole('heading', { level: 1, name: /terms of service/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /acceptable use/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /eligibility and accounts/i })).toBeInTheDocument();
    expect(
      screen.getByText(/you must be at least 18 years old to create an account/i),
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /logging and data use/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /no warranty/i })).toBeInTheDocument();
    expect(
      screen.getByText(/all prompts and responses may be logged for research purposes/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/this page is not legal advice/i)).toBeInTheDocument();
  });

  it('describes experimental service limits, mutable quotas, sanitization, and output review', () => {
    render(<TermsPage />);

    expect(screen.getByText(/experimental research service/i)).toBeInTheDocument();
    expect(screen.getByText(/no performance guarantee/i)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /quotas and limits/i })).toBeInTheDocument();
    expect(screen.getByText(/quotas.*may change based on usage/i)).toBeInTheDocument();
    expect(
      screen.getByText(/sanitize all text before analysis where feasible/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/sanitization is not a guarantee/i)).toBeInTheDocument();
    expect(
      screen.getByText(/anonymized data derived from them.*may be published or open-sourced/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/must review model outputs before relying on them/i),
    ).toBeInTheDocument();
  });
});
