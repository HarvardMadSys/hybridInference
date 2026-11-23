// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ProjectsAndKeys, type ProjectEntry } from './ProjectsAndKeys';

afterEach(cleanup);

const PROJECTS: ProjectEntry[] = [
  {
    id: 'p1',
    name: 'Default',
    isDefault: true,
    figures: 'USD 0.00 this month',
    keys: [],
  },
  {
    id: 'p2',
    name: 'CI',
    isDefault: false,
    figures: 'USD 1.00 this month',
    keys: [
      { id: 'k1', name: 'runner', figures: 'USD 1.00 this month' },
      { id: 'k2', name: null, statusNote: 'revoked', figures: 'USD 0.00 this month' },
    ],
  },
];

describe('the projects and keys of an account', () => {
  it('lists each project with its figures and its keys', () => {
    render(<ProjectsAndKeys projects={PROJECTS} />);

    expect(screen.getByText('Projects and keys')).toBeInTheDocument();
    const ci = screen.getByRole('region', { name: 'CI' });
    expect(within(ci).getAllByText('USD 1.00 this month')).toHaveLength(2);
    expect(within(ci).getByText('runner')).toBeInTheDocument();
    expect(within(ci).getByText('Unnamed key')).toBeInTheDocument();
    expect(within(ci).getByText('revoked')).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: 'Default' })).getByText('No keys.'),
    ).toBeInTheDocument();
  });

  it('shows the masked key only when it is given one', () => {
    const { container, rerender } = render(<ProjectsAndKeys projects={PROJECTS} />);
    expect(container.querySelector('code')).toBeNull();

    const masked = PROJECTS.map((project) => ({
      ...project,
      keys: project.keys.map((key) => ({ ...key, masked: 'sk-…abcd' })),
    }));
    rerender(<ProjectsAndKeys projects={masked} />);
    expect(container.querySelectorAll('code')).toHaveLength(2);
  });

  it('draws the control of a key at the end of its line', () => {
    const acted = PROJECTS.map((project) => ({
      ...project,
      keys: project.keys.map((key) => ({
        ...key,
        action: <button type="button">Revoke {key.name ?? 'key'}</button>,
      })),
    }));
    render(<ProjectsAndKeys projects={acted} />);

    expect(screen.getByRole('button', { name: 'Revoke runner' })).toBeInTheDocument();
  });

  it('marks the default project unless its name says so, and sets archived keys apart', () => {
    render(
      <ProjectsAndKeys
        projects={[{ ...PROJECTS[1], isDefault: true }]}
        archivedKeys={[{ id: 'k9', name: 'old', figures: 'USD 0.00 this month' }]}
      />,
    );

    expect(within(screen.getByRole('region', { name: 'CI' })).getByText('Default')).toBeVisible();
    const archived = screen.getByRole('region', { name: 'Archived projects' });
    expect(within(archived).getByText('old')).toBeInTheDocument();
  });

  it('says that the list is on its way, or why it could not be read', () => {
    const { rerender } = render(<ProjectsAndKeys projects={null} />);
    expect(screen.getByText('Loading…')).toBeInTheDocument();

    rerender(<ProjectsAndKeys projects={null} error="Database not configured" />);
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
    expect(screen.getByText('Database not configured')).toBeInTheDocument();
  });
});
