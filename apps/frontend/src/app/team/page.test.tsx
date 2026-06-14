// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import TeamPage from './page';

describe('TeamPage', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the team heading and lead member', () => {
    render(<TeamPage />);

    expect(screen.getByRole('heading', { level: 1, name: /^team$/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /juncheng yang/i })).toBeInTheDocument();
    expect(screen.getByText(/^lead$/i)).toBeInTheDocument();
    expect(screen.getByText(/assistant professor at harvard university/i)).toBeInTheDocument();
  });

  it('renders the research interns with their affiliations', () => {
    render(<TeamPage />);

    expect(screen.getByRole('heading', { name: /murphy tian/i })).toBeInTheDocument();
    expect(screen.getByText(/undergraduate at university of toronto/i)).toBeInTheDocument();

    expect(screen.getByRole('heading', { name: /haoran ni/i })).toBeInTheDocument();
    expect(screen.getByText(/undergraduate at nju/i)).toBeInTheDocument();

    expect(screen.getAllByText(/research intern at harvard university/i)).toHaveLength(2);
  });
});
