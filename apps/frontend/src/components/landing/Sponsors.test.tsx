// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Sponsors } from './Sponsors';

// Sponsors are distribution content (supplied via NEXT_PUBLIC_SPONSORS_JSON),
// so the test provides them rather than relying on a shipped default.
vi.mock('@/config/branding', () => ({
  branding: {
    sponsors: [
      {
        name: 'NVIDIA',
        alt: 'NVIDIA logo',
        src: '/sponsors/nvidia.svg',
        className: 'h-10 sm:h-12',
        width: 975,
        height: 180,
      },
      {
        name: 'Harvard SEAS',
        alt: 'Harvard SEAS logo',
        src: '/sponsors/harvard-seas.svg',
        className: 'h-12 sm:h-14',
        width: 307,
        height: 86,
      },
    ],
  },
}));

describe('Sponsors', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the sponsors heading and both sponsor logos', () => {
    render(<Sponsors />);

    expect(screen.getByText('Sponsors')).toBeInTheDocument();
    expect(screen.getByAltText('NVIDIA logo')).toBeInTheDocument();
    expect(screen.getByAltText('Harvard SEAS logo')).toBeInTheDocument();
  });

  it('shows sponsor logos in full color', () => {
    render(<Sponsors />);

    [screen.getByAltText('NVIDIA logo'), screen.getByAltText('Harvard SEAS logo')].forEach(
      (logo) => {
        expect(logo).not.toHaveClass('grayscale');
        expect(logo).not.toHaveClass('opacity-70');
      },
    );
  });
});
