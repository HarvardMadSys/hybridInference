// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import TermsPage from './page';

describe('TermsPage', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders legal-style terms for service use, logging, and disclaimers', () => {
    render(<TermsPage />);

    expect(
      screen.getByRole('heading', { level: 1, name: /terms of service/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /acceptable use/i })).toBeInTheDocument();
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
