// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Sponsors } from './Sponsors';

// Sponsors are runtime distribution content, so the test provides them rather
// than relying on a shipped default.
vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => ({
    sponsors: [
      {
        name: 'Example Labs',
        alt: 'Example Labs logo',
        src: '/site-assets/sponsors/example-labs.svg',
        className: 'h-10 sm:h-12',
        width: 975,
        height: 180,
      },
      {
        name: 'Example Institute',
        alt: 'Example Institute logo',
        src: 'https://assets.example.test/example-institute.svg',
        className: 'h-12 sm:h-14',
        width: 307,
        height: 86,
      },
    ],
  }),
}));

describe('Sponsors', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the sponsors heading and both sponsor logos', () => {
    render(<Sponsors />);

    expect(screen.getByText('Sponsors')).toBeInTheDocument();
    expect(screen.getByAltText('Example Labs logo')).toBeInTheDocument();
    expect(screen.getByAltText('Example Institute logo')).toBeInTheDocument();
    expect(screen.getByAltText('Example Labs logo')).toHaveAttribute(
      'src',
      '/site-assets/sponsors/example-labs.svg',
    );
    expect(screen.getByAltText('Example Institute logo')).toHaveAttribute(
      'src',
      'https://assets.example.test/example-institute.svg',
    );
  });

  it('shows sponsor logos in full color', () => {
    render(<Sponsors />);

    [
      screen.getByAltText('Example Labs logo'),
      screen.getByAltText('Example Institute logo'),
    ].forEach((logo) => {
      expect(logo).not.toHaveClass('grayscale');
      expect(logo).not.toHaveClass('opacity-70');
    });
  });
});
