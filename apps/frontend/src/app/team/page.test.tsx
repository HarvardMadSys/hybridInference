// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import TeamPage from './page';

// The roster is distribution content (NEXT_PUBLIC_TEAM_JSON); upstream ships
// none, so the page is exercised with an explicit one.
//
// Invented people, deliberately. The previous fixture used the real roster —
// names, job titles, employers, personal websites and photo URLs — as test
// data in a repository that is about to be published. What the page has to be
// shown doing is rendering whatever roster it is handed, and a real person's
// affiliation proves nothing about that.
vi.mock('@/config/branding', () => ({
  branding: {
    orgName: 'Example Org',
    orgUrl: 'https://org.example.test',
    team: [
      {
        name: 'Ada Example',
        affiliations: ['Principal Investigator at Example University'],
        badge: 'Lead',
        image: 'https://images.example.test/ada.jpg',
      },
      {
        name: 'Blake Sample',
        affiliations: ['Research Intern at Example University', 'Undergraduate at Example College'],
        badge: 'Core developer',
        image: '/team/blake-sample.jpg',
        website: 'https://blake.example.test/',
      },
      {
        name: 'Cameron Placeholder',
        affiliations: ['Research Intern at Example University', 'Undergraduate at Example Institute'],
      },
    ],
  },
}));

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

    expect(
      screen.getByRole('heading', { level: 1, name: /the people behind/i }),
    ).toBeInTheDocument();

    const leadCard = cardFor(/ada example/i);
    expect(within(leadCard).getByText(/^lead$/i)).toBeInTheDocument();
    expect(
      within(leadCard).getByText(/principal investigator at example university/i),
    ).toBeInTheDocument();

    const photo = within(leadCard).getByAltText(/photo of ada example/i);
    expect(photo).toHaveAttribute('src', expect.stringContaining('images.example.test'));
  });

  it('renders the research interns with badges, affiliations, photos, and placeholder avatars', () => {
    render(<TeamPage />);

    const developerCard = cardFor(/blake sample/i);
    expect(within(developerCard).getByText(/^core developer$/i)).toBeInTheDocument();
    expect(within(developerCard).getByRole('link', { name: /blake sample/i })).toHaveAttribute(
      'href',
      'https://blake.example.test/',
    );
    expect(
      within(developerCard).getByText(/research intern at example university/i),
    ).toBeInTheDocument();
    expect(
      within(developerCard).getByText(/undergraduate at example college/i),
    ).toBeInTheDocument();
    const developerPhoto = within(developerCard).getByAltText(/photo of blake sample/i);
    expect(developerPhoto).toHaveAttribute('src', expect.stringContaining('blake-sample.jpg'));

    const internCard = cardFor(/cameron placeholder/i);
    expect(
      within(internCard).getByText(/research intern at example university/i),
    ).toBeInTheDocument();
    expect(
      within(internCard).getByText(/undergraduate at example institute/i),
    ).toBeInTheDocument();
    expect(
      within(internCard).getByLabelText(/placeholder avatar for cameron placeholder/i),
    ).toBeInTheDocument();
  });
});
