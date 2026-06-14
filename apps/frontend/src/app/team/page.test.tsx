// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import TeamPage from './page';

describe('TeamPage', () => {
  afterEach(() => {
    cleanup();
  });

  const cardFor = (name: RegExp): HTMLElement => {
    const card = screen.getByRole('heading', { name }).closest('li');
    expect(card).not.toBeNull();
    return card as HTMLElement;
  };

  it('renders the team heading and lead member with a photo', () => {
    render(<TeamPage />);

    expect(screen.getByRole('heading', { level: 1, name: /^team$/i })).toBeInTheDocument();

    const junchengCard = cardFor(/juncheng yang/i);
    expect(within(junchengCard).getByText(/^lead$/i)).toBeInTheDocument();
    expect(
      within(junchengCard).getByText(/assistant professor at harvard university/i),
    ).toBeInTheDocument();

    const photo = within(junchengCard).getByAltText(/photo of juncheng yang/i);
    expect(photo).toHaveAttribute('src', expect.stringContaining('junchengyang.com'));
  });

  it('renders the research interns with badges, affiliations, and placeholder avatars', () => {
    render(<TeamPage />);

    const murphyCard = cardFor(/murphy tian/i);
    expect(within(murphyCard).getByText(/^core developer$/i)).toBeInTheDocument();
    expect(
      within(murphyCard).getByText(/research intern at harvard university/i),
    ).toBeInTheDocument();
    expect(
      within(murphyCard).getByText(/undergraduate at university of toronto/i),
    ).toBeInTheDocument();
    expect(
      within(murphyCard).getByLabelText(/placeholder avatar for murphy tian/i),
    ).toBeInTheDocument();

    const haoranCard = cardFor(/haoran ni/i);
    expect(
      within(haoranCard).getByText(/research intern at harvard university/i),
    ).toBeInTheDocument();
    expect(within(haoranCard).getByText(/undergraduate at nju/i)).toBeInTheDocument();
    expect(
      within(haoranCard).getByLabelText(/placeholder avatar for haoran ni/i),
    ).toBeInTheDocument();
  });
});
