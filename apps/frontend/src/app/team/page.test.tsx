// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
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

    const cardFor = (name: RegExp): HTMLElement => {
      const card = screen.getByRole('heading', { name }).closest('li');
      expect(card).not.toBeNull();
      return card as HTMLElement;
    };

    const murphyCard = cardFor(/murphy tian/i);
    expect(
      within(murphyCard).getByText(/research intern at harvard university/i),
    ).toBeInTheDocument();
    expect(
      within(murphyCard).getByText(/undergraduate at university of toronto/i),
    ).toBeInTheDocument();

    const haoranCard = cardFor(/haoran ni/i);
    expect(
      within(haoranCard).getByText(/research intern at harvard university/i),
    ).toBeInTheDocument();
    expect(within(haoranCard).getByText(/undergraduate at nju/i)).toBeInTheDocument();
  });
});
